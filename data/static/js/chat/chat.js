/* chat.js */

let oldestLoadedMessageId = null;
let limit = 25;
let initialLoad = true;
let isLoading = false;
let isLoadingConversations = false;
let allMessagesLoaded = false;
let allConversationsLoaded = false;
let oldestLoadedActivity = null;
let oldestLoadedId = null;
let currentAbortController = null;
let conversationViewGeneration = 0;
let activeMessageLoad = null;
let activeBookmarksLoad = null;
let conversationDetailsController = null;
let controller = null;
let currentTimer = null;
let lastSelectedConversationId = null;
var botname = "bot";
const limitMessage = 25;
const processedMessageIds = new Set();
let currentThinkingBudget = 0;
let isCurrentConversationLocked = false;
let currentConversationIncognito = false;
let userStopped = false;
let currentProviderHealth = null;
let currentMemoryHealth = null;

function conversationIdsMatch(left, right) {
    return left !== null && left !== undefined &&
        right !== null && right !== undefined &&
        String(left) === String(right);
}

function isCurrentConversationView(conversationId, generation) {
    return generation === conversationViewGeneration &&
        conversationIdsMatch(currentConversationId, conversationId);
}

function abortActiveMessageLoad() {
    if (activeMessageLoad?.controller) {
        activeMessageLoad.controller.abort();
    } else if (currentAbortController) {
        currentAbortController.abort();
    }
    activeMessageLoad = null;
    currentAbortController = null;
    isLoading = false;
}

function releaseActiveMessageLoad(loadState, enableControls = false) {
    if (activeMessageLoad !== loadState) {
        return false;
    }
    activeMessageLoad = null;
    if (currentAbortController === loadState.controller) {
        currentAbortController = null;
    }
    isLoading = false;
    if (enableControls &&
        isCurrentConversationView(loadState.conversationId, loadState.generation)) {
        enableInputControls();
    }
    return true;
}

function abortActiveBookmarksLoad() {
    if (activeBookmarksLoad?.controller) {
        activeBookmarksLoad.controller.abort();
    }
    activeBookmarksLoad = null;
}

function beginConversationViewTransition() {
    conversationViewGeneration += 1;
    abortActiveMessageLoad();
    abortActiveBookmarksLoad();
    if (conversationDetailsController) {
        conversationDetailsController.abort();
        conversationDetailsController = null;
    }
    if (window.modelSelector?.cancelPendingRequest) {
        window.modelSelector.cancelPendingRequest();
    }
    if (window.extensionSelector?.cancelPendingRequest) {
        window.extensionSelector.cancelPendingRequest();
    }
    return conversationViewGeneration;
}

// Auto-scroll state management
let isUserScrolledUp = false;
const SCROLL_THRESHOLD = 100; // px from bottom to consider "locked" to bottom

function isNearBottom(element) {
    return element.scrollHeight - element.scrollTop - element.clientHeight < SCROLL_THRESHOLD;
}

function scrollToBottomIfNeeded() {
    const chatWindow = document.getElementById('chat-window');
    if (!isUserScrolledUp) {
        chatWindow.scrollTop = chatWindow.scrollHeight;
    }
}

// Web Search Toggle state
let webSearchEnabled = true;   // User preference (default ON)
let webSearchAllowed = true;   // Prompt allows web search
let webSearchForced = false;   // Prompt forces web search always on

function providerHealthShouldSurface(health) {
    return !!(health && ['suspected', 'degraded', 'recovering'].includes(health.status));
}

function providerHealthFallbackMessage(health) {
    if (!health) return '';
    const providerName = health.provider_name || 'the selected AI provider';
    if (health.source === 'official_status') {
        return `${providerName} reports an API incident. This model may fail temporarily or respond more slowly.`;
    }
    return `We are detecting recent connection errors with ${providerName}. This model may fail temporarily or take longer than usual.`;
}

function setCurrentProviderHealth(health) {
    currentProviderHealth = health || null;
    renderProviderHealthBanner();
}

function renderProviderHealthBanner() {
    const banner = document.getElementById('provider-health-banner');
    const textEl = document.getElementById('provider-health-banner-text');
    if (!banner || !textEl) return;

    if (!providerHealthShouldSurface(currentProviderHealth)) {
        banner.style.display = 'none';
        textEl.textContent = '';
        return;
    }

    textEl.textContent = currentProviderHealth.message || providerHealthFallbackMessage(currentProviderHealth);
    banner.style.display = 'flex';
}

function escapeProviderHealthHtml(value) {
    if (typeof escapeHtml === 'function') {
        return escapeHtml(value);
    }
    return String(value || '').replace(/[&<>"']/g, (char) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    }[char]));
}

function showProviderAwareError(title, message, source = null) {
    const health = source?.provider_health || source || currentProviderHealth;
    if (!providerHealthShouldSurface(health)) {
        NotificationModal.error(title, String(message || ''));
        return;
    }

    setCurrentProviderHealth(health);
    const note = health.message || providerHealthFallbackMessage(health);
    const html = `${escapeProviderHealthHtml(message || '')}<span class="provider-health-modal-note">${escapeProviderHealthHtml(note)}</span>`;
    NotificationModal.error(title, html, { allowHtml: true });
}

function memoryHealthShouldSurface(health) {
    if (!health || health.enabled === false) return false;
    if (health.should_surface === false) return false;
    return ['suspected', 'degraded', 'unavailable'].includes(health.status);
}

function memoryHealthFallbackMessage(health) {
    if (!health) return '';
    if (health.status === 'unavailable') {
        return 'Memory is temporarily unavailable. Replies may not use saved long-term memory.';
    }
    return 'Memory is temporarily degraded. Replies may not use saved long-term memory.';
}

function setCurrentMemoryHealth(health) {
    currentMemoryHealth = health || null;
    renderMemoryHealthBanner();
}

function renderMemoryHealthBanner() {
    const banner = document.getElementById('memory-health-banner');
    const textEl = document.getElementById('memory-health-banner-text');
    if (!banner || !textEl) return;

    if (!memoryHealthShouldSurface(currentMemoryHealth)) {
        banner.style.display = 'none';
        textEl.textContent = '';
        return;
    }

    textEl.textContent = currentMemoryHealth.message || memoryHealthFallbackMessage(currentMemoryHealth);
    banner.style.display = 'flex';
}

async function refreshMemoryHealthBanner() {
    try {
        const response = await secureFetch('/api/user/memory-status', { method: 'GET' });
        if (!response || !response.ok) return;
        const data = await response.json();
        setCurrentMemoryHealth(data.memory_health || data.health || null);
    } catch (error) {
        console.debug('Memory health check failed:', error);
    }
}

// =============================================
// API Key Mode Manager
// =============================================
function getCurrentConversationLlmIdForUi() {
    const selectorLlmId = parseInt(window.modelSelector?.currentLlmId, 10);
    if (Number.isInteger(selectorLlmId) && selectorLlmId > 0) {
        return selectorLlmId;
    }
    const selected = window.selectedChat &&
        conversationIdsMatch(
            window.selectedChat.dataset?.conversationId,
            typeof currentConversationId !== 'undefined' ? currentConversationId : null
        )
        ? window.selectedChat
        : null;
    const selectedLlmId = parseInt(
        selected?.dataset?.llmId || selected?._conversationData?.llm_id,
        10
    );
    if (Number.isInteger(selectedLlmId) && selectedLlmId > 0) {
        return selectedLlmId;
    }
    const embedded = typeof embeddedInitialConversations !== 'undefined' &&
        Array.isArray(embeddedInitialConversations)
        ? embeddedInitialConversations.find(conversation =>
            conversationIdsMatch(
                conversation.id,
                typeof currentConversationId !== 'undefined'
                    ? currentConversationId
                    : null
            ))
        : null;
    const embeddedLlmId = parseInt(embedded?.llm_id, 10);
    return Number.isInteger(embeddedLlmId) && embeddedLlmId > 0
        ? embeddedLlmId
        : null;
}

function currentConversationUsesChatGptSubscription() {
    const llmId = getCurrentConversationLlmIdForUi();
    if (!llmId || typeof availableModels === 'undefined' ||
        !Array.isArray(availableModels)) {
        return false;
    }
    return availableModels.some(model =>
        Number(model.id) === llmId && model.machine === 'GPTSub'
    );
}

window.currentConversationUsesChatGptSubscription =
    currentConversationUsesChatGptSubscription;

const ApiKeyManager = {
    mode: 'both_prefer_own',
    canSend: false,
    requiresOwn: true,
    hasOwn: false,
    initialized: false,

    initializeFromPage() {
        if (this.initialized) return;
        this.mode = typeof globalThis.apiKeyMode !== 'undefined'
            ? globalThis.apiKeyMode
            : 'both_prefer_own';
        this.canSend = typeof globalThis.canSendMessages !== 'undefined'
            ? globalThis.canSendMessages === true
            : false;
        this.requiresOwn = typeof globalThis.requiresOwnKeys !== 'undefined'
            ? globalThis.requiresOwnKeys === true
            : true;
        this.hasOwn = typeof globalThis.hasOwnKeys !== 'undefined'
            ? globalThis.hasOwnKeys === true
            : false;
        this.initialized = true;
    },

    /**
     * Check if user can send messages based on API key configuration
     * @returns {boolean}
     */
    canSendMessages: function() {
        // chat.js loads before the template's inline page context. Hydrate on
        // first use (after DOMContentLoaded) instead of caching permissive
        // defaults while those globals are still undefined.
        this.initializeFromPage();
        return this.canSend || currentConversationUsesChatGptSubscription();
    },

    /**
     * Update the API key status by fetching from server
     * @returns {Promise<void>}
     */
    async refreshStatus() {
        try {
            const response = await fetch('/api/user/api-key-status');
            if (response.ok) {
                const data = await response.json();
                this.mode = data.mode;
                this.canSend = data.can_send_messages;
                this.requiresOwn = data.requires_own_keys;
                this.hasOwn = data.has_own_keys;
                this.initialized = true;

                // Update UI based on new status
                this.updateUI();
            }
        } catch (error) {
            console.error('Failed to refresh API key status:', error);
        }
    },

    /**
     * Update UI elements based on current API key status
     */
    updateUI() {
        const banner = document.getElementById('api-keys-required-banner');
        const inputContainer = document.getElementById('message-input-container');

        if (this.canSendMessages()) {
            // Hide banner and enable input
            if (banner) banner.style.display = 'none';
            if (inputContainer) inputContainer.removeAttribute('data-disabled');
        } else {
            // Show banner and disable input
            if (banner) banner.style.display = 'block';
            if (inputContainer) inputContainer.setAttribute('data-disabled', 'true');
        }
        if (typeof window.updateConversationBalanceAvailability === 'function') {
            window.updateConversationBalanceAvailability();
        }
    },

    /**
     * Handle API key error response from server
     * @param {Object} errorData - Error data from server
     */
    handleApiKeyError(errorData) {
        if (errorData.error === 'api_keys_required' || errorData.action === 'configure_api_keys') {
            NotificationModal.confirm(
                'API Keys Required',
                'You need to configure your API keys to use AI services. Would you like to go to the API credentials page?',
                () => {
                    window.location.href = '/api-credentials';
                },
                null,
                { confirmText: 'Configure', type: 'warning' }
            );
        }
    }
};

const COLLAPSIBLE_LINE_THRESHOLD = 11;

function applyCollapsibleUserMsg(divText, messageContent) {
    requestAnimationFrame(() => {
        const lineHeight = parseFloat(getComputedStyle(divText).lineHeight);
        const maxCollapsedHeight = lineHeight * COLLAPSIBLE_LINE_THRESHOLD;
        if (divText.scrollHeight > maxCollapsedHeight + lineHeight) {
            divText.classList.add('user-msg-collapsed');
            divText.style.setProperty('--collapsed-height', maxCollapsedHeight + 'px');

            const toggleBtn = document.createElement('button');
            toggleBtn.className = 'user-msg-toggle';
            toggleBtn.textContent = 'Show more';
            toggleBtn.addEventListener('click', () => {
                const isCollapsed = divText.classList.toggle('user-msg-collapsed');
                toggleBtn.textContent = isCollapsed ? 'Show more' : 'Show less';
                if (isCollapsed) {
                    const msg = divText.closest('.message');
                    if (msg.getBoundingClientRect().top < 0) {
                        msg.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }
                }
            });
            messageContent.insertBefore(toggleBtn, divText.nextSibling);
        }
    });
}

function applyCollapsibleCodeBlocks(container) {
    requestAnimationFrame(() => {
        container.querySelectorAll('.code-block').forEach(block => {
            if (block.querySelector('.code-block-toggle')) return;
            const pre = block.querySelector('pre');
            if (!pre) return;
            const code = pre.querySelector('code');
            if (!code) return;
            const lineHeight = parseFloat(getComputedStyle(code).lineHeight);
            const maxCollapsedHeight = lineHeight * COLLAPSIBLE_LINE_THRESHOLD;
            if (pre.scrollHeight > maxCollapsedHeight + lineHeight) {
                pre.classList.add('code-block-collapsed');
                pre.style.setProperty('--collapsed-height', maxCollapsedHeight + 'px');

                const toggleBtn = document.createElement('button');
                toggleBtn.className = 'code-block-toggle';
                toggleBtn.textContent = 'Show more';
                toggleBtn.addEventListener('click', () => {
                    const isCollapsed = pre.classList.toggle('code-block-collapsed');
                    toggleBtn.textContent = isCollapsed ? 'Show more' : 'Show less';
                    if (isCollapsed) {
                        const msg = block.closest('.message');
                        if (msg && msg.getBoundingClientRect().top < 0) {
                            msg.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        }
                    }
                });
                block.appendChild(toggleBtn);
            }
        });
    });
}

function buildSourcesBlock(citations) {
    const list = document.createElement('div');
    list.className = 'sources-list';

    const seen = new Set();
    let count = 0;
    citations.forEach(function(c) {
        if (!c.url || seen.has(c.url) || !/^https?:\/\//i.test(c.url)) return;
        seen.add(c.url);
        count++;

        const item = document.createElement('a');
        item.className = 'source-item';
        item.href = c.url;
        item.target = '_blank';
        item.rel = 'noopener noreferrer';

        let domain = '';
        try { domain = new URL(c.url).hostname.replace('www.', ''); } catch(e) {}

        const favicon = document.createElement('img');
        favicon.className = 'source-favicon';
        favicon.src = 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(domain) + '&sz=16';
        favicon.alt = '';
        favicon.loading = 'lazy';
        favicon.onerror = function() { this.style.display = 'none'; };

        const info = document.createElement('div');
        info.className = 'source-info';

        const title = document.createElement('div');
        title.className = 'source-title';
        title.textContent = c.title || domain;

        const domainEl = document.createElement('div');
        domainEl.className = 'source-domain';
        domainEl.textContent = domain;

        info.appendChild(title);
        info.appendChild(domainEl);
        item.appendChild(favicon);
        item.appendChild(info);
        list.appendChild(item);
    });

    if (count === 0) return null;

    const block = document.createElement('div');
    block.className = 'sources-block';

    const header = document.createElement('div');
    header.className = 'sources-header';
    header.innerHTML = '<i class="fas fa-chevron-right sources-chevron"></i> ' + count + ' source' + (count !== 1 ? 's' : '');
    header.addEventListener('click', function() {
        block.classList.toggle('expanded');
    });

    block.appendChild(header);
    block.appendChild(list);
    return block;
}

function renderMarkdownIntoElement(targetElement, markdownText) {
    const text = typeof markdownText === 'string' ? markdownText : String(markdownText || '');
    let processedHTML = DOMPurify.sanitize(marked.parse(text));
    processedHTML = formatCodeBlocks(processedHTML);
    targetElement.innerHTML = processedHTML;

    targetElement.querySelectorAll('pre code').forEach((el) => {
        hljs.highlightElement(el);
    });
}

function getActiveMultiAiSlideText(messageElement) {
    if (!messageElement || typeof messageElement.querySelector !== 'function') {
        return '';
    }
    const carousel = messageElement.querySelector('.multi-ai-carousel');
    if (!carousel) return '';

    const api = carousel._multiAiApi;
    if (api && typeof api.getActiveText === 'function') {
        return api.getActiveText();
    }

    const activeContent = carousel.querySelector('.multi-ai-slide.active .multi-ai-slide-content');
    return activeContent ? activeContent.textContent.trim() : '';
}

function createMultiAiCarousel(models = []) {
    const normalizedModels = (Array.isArray(models) ? models : []).map((model, index) => {
        let llmId = Number.parseInt(model?.llm_id, 10);
        if (!Number.isFinite(llmId)) {
            llmId = -(index + 1);
        }

        return {
            llm_id: llmId,
            machine: model?.machine || 'AI',
            model: model?.model || `Model ${index + 1}`,
        };
    });

    const carousel = document.createElement('div');
    carousel.classList.add('multi-ai-carousel');
    carousel.tabIndex = 0;

    const header = document.createElement('div');
    header.classList.add('multi-ai-header');

    const label = document.createElement('span');
    label.classList.add('multi-ai-label');
    label.textContent = 'Multi-AI Compare';

    const nav = document.createElement('div');
    nav.classList.add('multi-ai-nav');

    const prevBtn = document.createElement('button');
    prevBtn.type = 'button';
    prevBtn.classList.add('multi-ai-nav-btn', 'multi-ai-prev');
    prevBtn.innerHTML = '<i class="fas fa-chevron-left"></i>';

    const indicator = document.createElement('div');
    indicator.classList.add('multi-ai-indicator');

    const nextBtn = document.createElement('button');
    nextBtn.type = 'button';
    nextBtn.classList.add('multi-ai-nav-btn', 'multi-ai-next');
    nextBtn.innerHTML = '<i class="fas fa-chevron-right"></i>';

    nav.appendChild(prevBtn);
    nav.appendChild(indicator);
    nav.appendChild(nextBtn);
    header.appendChild(label);
    header.appendChild(nav);

    const slidesContainer = document.createElement('div');
    slidesContainer.classList.add('multi-ai-slides-container');

    carousel.appendChild(header);
    carousel.appendChild(slidesContainer);

    const slides = [];
    const textsByLlmId = new Map();
    let activeIndex = 0;
    let globalErrorEl = null;

    function getSlideState(llmId) {
        const normalized = Number.parseInt(llmId, 10);
        return slides.find((slide) => slide.llmId === normalized) || null;
    }

    function setActiveSlide(nextIndex) {
        if (!slides.length) return;
        if (nextIndex < 0 || nextIndex >= slides.length) return;

        activeIndex = nextIndex;
        slides.forEach((slide, idx) => {
            slide.element.classList.toggle('active', idx === activeIndex);
            slide.dot.classList.toggle('active', idx === activeIndex);
            slide.dot.setAttribute('aria-pressed', idx === activeIndex ? 'true' : 'false');
        });

        prevBtn.disabled = activeIndex === 0;
        nextBtn.disabled = activeIndex === slides.length - 1;
    }

    function setSlideContent(llmId, content, append = false) {
        const slide = getSlideState(llmId);
        if (!slide) return false;

        const current = textsByLlmId.get(slide.llmId) || '';
        const newValue = append ? `${current}${content || ''}` : String(content || '');
        textsByLlmId.set(slide.llmId, newValue);

        renderMarkdownIntoElement(slide.paragraph, newValue);
        slide.element.classList.remove('error');
        applyCollapsibleCodeBlocks(slide.element);
        initializeNewImages(slide.element);
        return true;
    }

    function setSlideError(llmId, errorText) {
        const slide = getSlideState(llmId);
        if (!slide) return false;

        const text = String(errorText || 'Unknown error');
        textsByLlmId.set(slide.llmId, text);
        slide.paragraph.innerHTML = '';
        const errorSpan = document.createElement('span');
        errorSpan.classList.add('multi-ai-slide-error');
        errorSpan.textContent = text;
        slide.paragraph.appendChild(errorSpan);
        slide.element.classList.add('error', 'completed');
        return true;
    }

    function markSlideDone(llmId) {
        const slide = getSlideState(llmId);
        if (!slide) return false;
        slide.element.classList.add('completed');
        return true;
    }

    function setGlobalError(errorText) {
        const text = String(errorText || 'Unexpected error');
        if (!globalErrorEl) {
            globalErrorEl = document.createElement('div');
            globalErrorEl.classList.add('multi-ai-global-error');
            carousel.insertBefore(globalErrorEl, slidesContainer);
        }
        globalErrorEl.textContent = text;
    }

    function getActiveText() {
        if (!slides.length) return '';
        const activeSlide = slides[activeIndex];
        if (!activeSlide || !activeSlide.content || typeof activeSlide.content.textContent !== 'string') {
            return '';
        }
        return activeSlide.content.textContent.trim();
    }

    normalizedModels.forEach((model, index) => {
        const slide = document.createElement('div');
        slide.classList.add('multi-ai-slide');
        slide.dataset.llmId = String(model.llm_id);

        const slideHeader = document.createElement('div');
        slideHeader.classList.add('multi-ai-slide-header');

        const modelName = document.createElement('span');
        modelName.classList.add('multi-ai-model-name');
        modelName.textContent = model.model;

        const providerTag = document.createElement('span');
        providerTag.classList.add('multi-ai-provider-tag');
        providerTag.textContent = model.machine;

        const content = document.createElement('div');
        content.classList.add('multi-ai-slide-content');
        const paragraph = document.createElement('p');
        content.appendChild(paragraph);

        slideHeader.appendChild(modelName);
        slideHeader.appendChild(providerTag);
        slide.appendChild(slideHeader);
        slide.appendChild(content);
        slidesContainer.appendChild(slide);

        const dot = document.createElement('button');
        dot.type = 'button';
        dot.classList.add('multi-ai-dot');
        dot.setAttribute('aria-label', `View ${model.model}`);
        dot.addEventListener('click', () => setActiveSlide(index));
        indicator.appendChild(dot);

        slides.push({
            llmId: model.llm_id,
            element: slide,
            content,
            paragraph,
            dot,
        });
    });

    prevBtn.addEventListener('click', () => setActiveSlide(activeIndex - 1));
    nextBtn.addEventListener('click', () => setActiveSlide(activeIndex + 1));
    carousel.addEventListener('keydown', (event) => {
        if (event.key === 'ArrowLeft') {
            event.preventDefault();
            setActiveSlide(activeIndex - 1);
        } else if (event.key === 'ArrowRight') {
            event.preventDefault();
            setActiveSlide(activeIndex + 1);
        }
    });

    if (slides.length > 0) {
        setActiveSlide(0);
    } else {
        prevBtn.disabled = true;
        nextBtn.disabled = true;
    }

    carousel._multiAiApi = {
        setSlideContent,
        appendChunk: (llmId, chunk) => setSlideContent(llmId, chunk, true),
        setSlideError,
        markSlideDone,
        setGlobalError,
        setActiveByLlmId: (llmId) => {
            const target = getSlideState(llmId);
            if (!target) return false;
            const idx = slides.findIndex((slide) => slide.llmId === target.llmId);
            if (idx >= 0) setActiveSlide(idx);
            return idx >= 0;
        },
        getActiveText,
    };

    return carousel;
}

function addMessage(author, message, timestampInfo = null, isTemporary = false, messageObj = null, prepend = false, container = null, messageId = null, citations = null) {
    var divMessage = document.createElement('div');
    divMessage.classList.add('message', 'text-white', author === 'user' ? 'user' : 'bot');
    if (messageId) {
        divMessage.dataset.messageId = messageId;
    }

    var messageContentContainer = document.createElement('div');
    messageContentContainer.classList.add('message-content-container');

    var avatarContainer = createAvatar(author);
    messageContentContainer.appendChild(avatarContainer);

    var messageContent = document.createElement('div');
    messageContent.classList.add('message-content');

    if (isTemporary) {
        divMessage.classList.add('temporary-message');
    }

    let messageText = '';

    if (messageObj) {
        if (messageObj.type === 'text') {
            messageText = messageObj.text;
            var divText = document.createElement('p');
            if (author === 'user') {
                divText.classList.add('preserve-whitespace');
                divText.textContent = messageText;
            } else {
                let processedHTML = DOMPurify.sanitize(marked.parse(messageText));
                processedHTML = formatCodeBlocks(processedHTML);
                divText.innerHTML = processedHTML;

                divText.querySelectorAll('pre code').forEach((el) => {
                    hljs.highlightElement(el);
                });
            }
            messageContent.appendChild(divText);
            if (author === 'user') applyCollapsibleUserMsg(divText, messageContent);
            else applyCollapsibleCodeBlocks(divText);
        } else if (messageObj.type === 'multi_ai' && author === 'bot') {
            const rawResponses = Array.isArray(messageObj.responses) ? messageObj.responses : [];
            const normalizedResponses = rawResponses.map((response, index) => {
                let llmId = Number.parseInt(response?.llm_id, 10);
                if (!Number.isFinite(llmId)) {
                    llmId = -(index + 1);
                }
                return {
                    llm_id: llmId,
                    machine: response?.machine || 'AI',
                    model: response?.model || `Model ${index + 1}`,
                    content: String(response?.content || ''),
                    error: Boolean(response?.error),
                };
            });

            const carousel = createMultiAiCarousel(normalizedResponses);
            divMessage.classList.add('multi-ai-message');
            divMessage._multiAiCarousel = carousel;
            messageContent.appendChild(carousel);

            if (carousel._multiAiApi) {
                normalizedResponses.forEach((response) => {
                    if (response.error) {
                        carousel._multiAiApi.setSlideError(response.llm_id, response.content);
                    } else {
                        carousel._multiAiApi.setSlideContent(response.llm_id, response.content);
                    }
                    carousel._multiAiApi.markSlideDone(response.llm_id);
                });
            }

            messageText = normalizedResponses.find((r) => !r.error)?.content || normalizedResponses[0]?.content || '';
        } else if (messageObj.type === 'image_url') {
            var imgElement = document.createElement('img');
            imgElement.src = messageObj.url;
            imgElement.alt = messageObj.alt || '';
            imgElement.loading = 'lazy';
            imgElement.style.maxWidth = '256px';
            imgElement.style.maxHeight = '256px';
            imgElement.style.objectFit = 'contain';
            imgElement.style.cursor = 'pointer';
            imgElement.dataset.fullsize = messageObj.fullsize_url || messageObj.url.replace('_256.webp', '_fullsize.webp');
            imgElement.dataset.messageId = messageId;
            imgElement.dataset.attachmentRef = messageObj.attachment_ref || '';
            imgElement.onclick = function() {
                imageHandler.showFullsize(this.dataset.fullsize, this.dataset.messageId, this.dataset.attachmentRef);
            };
            messageContent.appendChild(imgElement);
        } else if (messageObj.type === 'video_url') {
            var videoElement = document.createElement('video');
            videoElement.src = messageObj.url;
            videoElement.controls = true;
            videoElement.style.maxWidth = '100%';
            videoElement.style.maxHeight = '480px';
            videoElement.style.width = 'auto';
            videoElement.style.height = 'auto';
            videoElement.preload = 'metadata';
            
            // Add poster image if available
            if (messageObj.poster) {
                videoElement.poster = messageObj.poster;
            }
            
            // Add accessibility attributes
            if (messageObj.alt) {
                videoElement.setAttribute('aria-label', messageObj.alt);
                videoElement.title = messageObj.alt;
            }
            
            messageContent.appendChild(videoElement);
        } else if (messageObj.type === 'document_url') {
            var pdfEl = document.createElement('a');
            pdfEl.href = messageObj.url;
            pdfEl.target = '_blank';
            pdfEl.rel = 'noopener noreferrer';
            pdfEl.className = 'chat-pdf-attachment';
            var badge = document.createElement('span');
            badge.className = 'pdf-badge';
            badge.textContent = 'PDF';
            var label = document.createElement('span');
            label.textContent = ' ' + messageObj.filename + ' (' + messageObj.pages + ' pages)';
            pdfEl.appendChild(badge);
            pdfEl.appendChild(label);
            messageContent.appendChild(pdfEl);
        } else if (messageObj && messageObj.type === 'text_file') {
            var textAttachment = document.createElement('div');
            textAttachment.className = 'chat-text-attachment';
            var badge = document.createElement('span');
            badge.className = 'text-badge';
            badge.textContent = 'TXT';
            var label = document.createElement('span');
            label.textContent = messageObj.filename + ' (' + messageObj.lines + ' lines)';
            textAttachment.appendChild(badge);
            textAttachment.appendChild(label);
            messageContent.appendChild(textAttachment);
        }
    } else {
        messageText = String(message);
        var divText = document.createElement('p');
        if (author === 'user') {
            divText.classList.add('preserve-whitespace');
            divText.textContent = messageText;
        } else {
            let processedHTML = DOMPurify.sanitize(marked.parse(messageText));
            processedHTML = formatCodeBlocks(processedHTML);
            divText.innerHTML = processedHTML;

            divText.querySelectorAll('pre code').forEach((el) => {
                hljs.highlightElement(el);
            });
        }
        messageContent.appendChild(divText);
        if (author === 'user') applyCollapsibleUserMsg(divText, messageContent);
        else applyCollapsibleCodeBlocks(divText);
    }

    if (timestampInfo) {
    
        var infoContainer = document.createElement('div');
        infoContainer.classList.add('message-info');
    
        var iconContainer = document.createElement('div');
        iconContainer.classList.add('icon-container');
    
        const audioIcon = document.createElement('i');
        audioIcon.classList.add('fa', 'fa-volume-up');
        audioIcon.dataset.baseIcon = 'fa-volume-up';
        audioIcon.style.cursor = 'pointer';
        audioIcon.style.display = 'inline';
        audioIcon.title = 'Read aloud';

        const resolveMessageText = () => {
            if (divMessage.classList.contains('multi-ai-message')) {
                const activeText = getActiveMultiAiSlideText(divMessage);
                if (activeText) return activeText;
            }
            return messageText;
        };

        audioIcon.dataset.id = currentConversationId;
        audioIcon.onclick = function() {
            textToSpeech(resolveMessageText(), user_id, currentConversationId, audioIcon, author);
        };

        const bookmarkIcon = document.createElement('i');
        bookmarkIcon.classList.add('fas', 'fa-bookmark', 'bookmark-icon');
        bookmarkIcon.style.cursor = 'pointer';
        bookmarkIcon.style.display = 'none';
        bookmarkIcon.title = 'Add to bookmarks';

        if (messageObj && messageObj.is_bookmarked) {
            bookmarkIcon.classList.add('bookmarked');
            bookmarkIcon.style.display = 'inline';
        }
    
        const conversationId = messageObj && messageObj.conversation_id ? messageObj.conversation_id : currentConversationId;
        bookmarkIcon.dataset.bookmarkConversationId = conversationId;
    
        bookmarkIcon.onclick = function() {
            const messageElement = this.closest('.message');
            const resolvedMessageId = messageElement ? messageElement.dataset.messageId : null;
            if (resolvedMessageId) {
                toggleBookmark(resolvedMessageId, currentConversationId, this);
            } else {
                console.error('Could not find message ID to mark as favorite');
            }
        };
    
        const copyIcon = document.createElement('i');
        copyIcon.classList.add('fas', 'fa-copy', 'copy-icon');
        copyIcon.style.cursor = 'pointer';
        copyIcon.style.display = 'none';
        copyIcon.title = 'Copy text';
        copyIcon.onclick = function() {
            copyToClipboard(resolveMessageText(), copyIcon);
        };
    
        iconContainer.appendChild(audioIcon);
        iconContainer.appendChild(bookmarkIcon);

        if (author === 'bot') {
            const rollbackIcon = document.createElement('i');
            rollbackIcon.classList.add('fas', 'fa-level-up-alt', 'rollback-icon');
            rollbackIcon.style.cursor = 'pointer';
            rollbackIcon.style.display = 'none';
            rollbackIcon.title = 'Start over from here';
            if (messageId) {
                rollbackIcon.setAttribute('data-message-id', messageId);
            }
            rollbackIcon.onclick = function() {
                rollbackConversation(this.getAttribute('data-message-id'), currentConversationId);
            };

            const chatTitle = document.querySelector('.chatbot-info h4').textContent;
            if (chatTitle !== "My Bookmarks") {
                iconContainer.appendChild(rollbackIcon);
            }
        }

        iconContainer.appendChild(copyIcon);

        // Branch icon - appears on all messages (user and bot), hidden in Bookmarks view
        const chatTitleForBranch = document.querySelector('.chatbot-info h4').textContent;
        if (chatTitleForBranch !== "My Bookmarks") {
            const branchIcon = document.createElement('i');
            branchIcon.classList.add('fas', 'fa-code-branch', 'branch-icon');
            branchIcon.style.cursor = 'pointer';
            branchIcon.style.display = 'none';
            branchIcon.title = 'Branch from here';
            if (messageId) {
                branchIcon.setAttribute('data-message-id', messageId);
            }
            branchIcon.onclick = function() {
                branchConversation(this.getAttribute('data-message-id'), currentConversationId);
            };
            iconContainer.appendChild(branchIcon);
        }

        // Add arrow icon if we are in "My Bookmarks"
        if (isMyBookmarksView() && conversationId) {
            const goToConversationIcon = document.createElement('i');
            goToConversationIcon.classList.add('fas', 'fa-arrow-right', 'go-to-conversation-icon');
            goToConversationIcon.style.cursor = 'pointer';
            goToConversationIcon.style.display = 'inline';
            goToConversationIcon.title = 'Go to conversation';

            goToConversationIcon.onclick = function() {
                continueConversation(conversationId, 'Chat', 'machine', false, messageId);
            };

            iconContainer.appendChild(goToConversationIcon);
        }
    
        var timeSpan = document.createElement('span');
    
        // Determine the correct date based on whether it's a new or loaded message
        var messageDate;
        if (timestampInfo.isNewMessage) {
            messageDate = new Date(timestampInfo.timestamp.originalUtc);
        } else {
            // Assume timestampInfo is directly the date string from database
            if (typeof timestampInfo.timestamp.originalUtc === 'string') {
                messageDate = new Date(timestampInfo.timestamp.originalUtc.replace(' ', 'T') + 'Z');
            } else {
                console.error('timestampInfo.timestamp.originalUtc is not a string:', timestampInfo.timestamp.originalUtc);
                timeSpan.textContent = 'Invalid date';
            }
        }


        // Verify if date is valid
        if (isNaN(messageDate.getTime())) {
            console.error('Invalid date:', timestampInfo);
            timeSpan.textContent = 'Invalid date';
        } else {
            // Convert UTC to local time
            var localDate = new Date(messageDate.toLocaleString('en-US', { timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone }));
    
            // Format the time in the user's local time zone
            var formattedTime = localDate.toLocaleTimeString(undefined, {
                hour: '2-digit',
                minute: '2-digit',
                hour12: false
            });
            timeSpan.textContent = formattedTime;
    
            // Format full date for title
            var formattedDate = localDate.toLocaleString(undefined, {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
                hour12: false
            });
            timeSpan.title = formattedDate;
    
        }
    
        infoContainer.appendChild(iconContainer);
        infoContainer.appendChild(timeSpan);

        // Render citations block for historical messages
        if (citations && citations.length > 0) {
            const sourcesBlock = buildSourcesBlock(citations);
            if (sourcesBlock) messageContent.appendChild(sourcesBlock);
        }

        messageContent.appendChild(infoContainer);
    }

    messageContentContainer.appendChild(messageContent);
    divMessage.appendChild(messageContentContainer);

    if (container) {
        container.appendChild(divMessage);
    } else {
        var chatMessagesContainer = document.getElementById('chat-messages-container');
        if (prepend) {
            chatMessagesContainer.insertBefore(divMessage, chatMessagesContainer.firstChild);
        } else {
            chatMessagesContainer.appendChild(divMessage);
            scrollToBottomIfNeeded();
        }
    }

    divMessage.addEventListener('mouseover', function() {
        const messageId = this.dataset.messageId;
        this.querySelectorAll('.fa-bookmark, .fa-copy, .fa-code-branch, .fa-level-up-alt, .fa-arrow-right').forEach(icon => {
            if (!icon.classList.contains('bookmarked')) {
                if (icon.classList.contains('fa-bookmark') && !messageId) {
                    icon.style.display = 'none';
                    return;
                }
                icon.style.display = 'inline';
            }
        });
    });

    divMessage.addEventListener('mouseout', function() {
        this.querySelectorAll('.fa-bookmark, .fa-copy, .fa-code-branch, .fa-level-up-alt, .fa-arrow-right').forEach(icon => {
            if (!icon.classList.contains('bookmarked')) {
                icon.style.display = 'none';
            }
        });
    });
    initializeNewImages(divMessage);

    return divMessage;
}

function rollbackConversation(messageId, conversationId) {
    if (!messageId) {
        console.error('No messageId provided for rollback');
        return;
    }
    NotificationModal.confirm(
        'Rollback Conversation',
        'Are you sure you want to roll back the conversation to this point? All messages after this will be deleted.',
        () => {
            fetch(`/api/conversations/${conversationId}/rollback`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ message_id: messageId })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    const chatMessagesContainer = document.getElementById('chat-messages-container');
                    const messages = Array.from(chatMessagesContainer.querySelectorAll('.message'));
                    let foundIndex = -1;

                    if (messageId) {
                        foundIndex = messages.findIndex(msg =>
                            msg.dataset.messageId === messageId ||
                            msg.querySelector(`[data-message-id="${messageId}"]`)
                        );
                    }

                    if (foundIndex !== -1) {
                        for (let i = messages.length - 1; i > foundIndex; i--) {
                            messages[i].remove();
                        }
                        // Point cursor to the oldest message still in DOM (index 0),
                        // not the rollback target, to avoid re-fetching messages above it
                        const oldestRendered = messages[0];
                        const msgId = oldestRendered?.dataset?.messageId || oldestRendered?.querySelector('[data-message-id]')?.dataset?.messageId;
                        if (msgId) {
                            oldestLoadedMessageId = parseInt(msgId);
                        }
                        allMessagesLoaded = false;
                        isCurrentConversationEmpty = false;
                    } else {
                        console.error('Message not found for rollback');
                    }
                } else {
                    NotificationModal.error('Rollback Failed', data.error || 'Could not roll back the conversation.');
                }
            })
            .catch(error => {
                console.error('Error rolling back conversation:', error);
                NotificationModal.error('Rollback Failed', 'An unexpected error occurred. Please try again.');
            });
        },
        null,
        { confirmText: 'Roll Back', cancelText: 'Cancel' }
    );
}

function branchConversation(messageId, conversationId) {
    if (!messageId) {
        console.error('No messageId provided for branch');
        return;
    }

    NotificationModal.confirm(
        'Branch Conversation',
        'Create a new conversation branching from this message? All messages up to this point will be copied.',
        () => {
            secureFetch(`/api/conversations/${conversationId}/branch`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message_id: parseInt(messageId) })
            })
            .then(response => {
                if (!response) return;
                return response.json();
            })
            .then(data => {
                if (!data) return;
                if (data.id) {
                    addConversationElement(data, data.name, null, true);
                    continueConversation(data.id, data.name, data.machine, false, null, data);
                    NotificationModal.success('Branch Created',
                        `New conversation created with ${data.messages_copied} messages.`);
                } else {
                    NotificationModal.error('Branch Failed', data.detail || data.error || 'Could not branch conversation.');
                }
            })
            .catch(error => {
                console.error('Error branching conversation:', error);
                NotificationModal.error('Branch Failed', 'An unexpected error occurred.');
            });
        },
        null,
        { confirmText: 'Branch', cancelText: 'Cancel' }
    );
}


function formatCodeBlocks(html) {
    const template = document.createElement('template');
    template.innerHTML = html;

    template.content.querySelectorAll('pre > code').forEach(code => {
        const pre = code.parentElement;
        if (!pre || pre.closest('.code-block')) return;

        let language = 'code';
        const match = (code.className || '').match(/language-(\S+)/);
        if (match) language = match[1];

        const rawText = code.textContent || '';
        const lines = rawText.replace(/^\n+|\n+$/g, '').split('\n');
        const commonIndent = lines.reduce((min, line) => {
            if (!line.trim()) return min;
            return Math.min(min, line.match(/^\s*/)[0].length);
        }, Infinity);
        if (commonIndent > 0 && commonIndent !== Infinity) {
            code.textContent = lines.map(line => line.slice(commonIndent)).join('\n');
        } else {
            code.textContent = lines.join('\n');
        }

        const wrapper = document.createElement('div');
        wrapper.className = 'code-block';

        const header = document.createElement('div');
        header.className = 'code-header';

        const langSpan = document.createElement('span');
        langSpan.className = 'code-language';
        langSpan.textContent = language;

        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'copy-button';
        copyBtn.setAttribute('onclick', 'copyCode(this)');
        copyBtn.setAttribute('aria-label', 'Copy code');
        copyBtn.title = 'Copy code';

        const copyIcon = document.createElement('i');
        copyIcon.className = 'fas fa-copy';
        copyBtn.appendChild(copyIcon);

        header.appendChild(langSpan);
        header.appendChild(copyBtn);
        wrapper.appendChild(header);

        pre.parentNode.insertBefore(wrapper, pre);
        wrapper.appendChild(pre);
    });

    return template.innerHTML;
}

function copyCode(button) {
    const codeBlock = button.closest('.code-block');
    const code = codeBlock.querySelector('code').innerText;
    navigator.clipboard.writeText(code).then(() => {
        button.innerHTML = '<i class="fas fa-check"></i>';
        setTimeout(() => {
            button.innerHTML = '<i class="fas fa-copy"></i>';
        }, 2000);
    });
}

function copyToClipboard(text, icon) {
    navigator.clipboard.writeText(text).then(function() {
        icon.classList.remove('fa-copy');
        icon.classList.add('fa-check');
        setTimeout(function() {
            icon.classList.remove('fa-check');
            icon.classList.add('fa-copy');
        }, 2000);
    }).catch(function(err) {
        console.error('Error copying text: ', err);
    });
}


function showNoChatTemplate() {
    var chatWindow = document.getElementById('chat-window');
    chatWindow.innerHTML = '<div class="no-chat-message">There is no selected chat or the chat has been deleted.</div>'; 
    document.getElementById('message-text').disabled = true;
    document.querySelector('#form-message button[type="submit"]').disabled = true;
}

function sendMessage(messageText, options = {}) {
    if (window.conversationModelIdentityUnknown) {
        NotificationModal.warning(
            'Model identity needs refresh',
            'Reload this conversation before sending so Aurvek can confirm the selected AI model.'
        );
        return false;
    }
    if (window.conversationModelMutationPending) {
        NotificationModal.warning(
            'Model update in progress',
            'Wait for the selected AI model to finish updating before sending.'
        );
        return false;
    }

    // Check if user can send messages based on API key configuration
    if (!ApiKeyManager.canSendMessages()) {
        ApiKeyManager.handleApiKeyError({ error: 'api_keys_required', action: 'configure_api_keys' });
        return false;
    }

    if (currentConversationId === null) {
        console.error('No conversation selected');
        return false;
    }
    const sendConversationId = currentConversationId;
    const expectedLlmId = Number.parseInt(window.modelSelector?.currentLlmId, 10);
    if (!Number.isInteger(expectedLlmId) || expectedLlmId <= 0) {
        window.conversationModelIdentityUnknown = true;
        NotificationModal.warning(
            'Model identity needs refresh',
            'Reload this conversation before sending so Aurvek can confirm the selected AI model.'
        );
        return false;
    }

    // Block send while images are being compressed
    if (compressionInProgress > 0) {
        NotificationModal.toast('Images are being compressed, please wait...', 'info', 2000);
        return false;
    }

    const outgoingFiles = Object.freeze(Array.from(
        Array.isArray(options.filesOverride) ? options.filesOverride : attachedFiles
    ));

    if (!messageText.trim() && (!outgoingFiles || outgoingFiles.length === 0)) {
        return false;
    }

    if (window.ChatWarmup && typeof window.ChatWarmup.cancel === 'function') {
        window.ChatWarmup.cancel();
    }

    // Reset auto-scroll state when user sends a message
    isUserScrolledUp = false;
    updateScrollBottomBtn();

    // Send the compressed message with pako
    const messageText_raw = messageText;
    const selectedMultiAiModels = window.multiAiManager?.enabled
        ? window.multiAiManager.selectedModels.map((model) => ({ ...model }))
        : [];
    const isMultiAiRequest = selectedMultiAiModels.length >= 2;

    let timestamp = new Date().toISOString();

    let userMessageElement = null;
    if (messageText.trim()) {
        userMessageElement = addMessage(
            'user',
            messageText_raw,
            { timestamp: convertToLocalTime(timestamp), isNewMessage: true },
            false,
            { type: 'text', text: messageText_raw }
        );
    }

    // Get the user message ID
    //getLastMessageId(userMessageElement)

    const multiAiLoadingText = isMultiAiRequest
        ? `Comparing ${selectedMultiAiModels.length} AI models...`
        : '';

    addLoadingIndicator(multiAiLoadingText);

    var formData = new FormData();
    formData.append('expected_llm_id', String(expectedLlmId));

    // Compress message with pako (maximum compression level)
    const compressedMessage = pako.deflate(messageText_raw, { level: 9 });
    formData.append('text_compressed', new Blob([compressedMessage], { type: 'application/octet-stream' }));
    formData.append('is_compressed', 'true'); // Indicate that message is compressed
    
    // Add thinking budget tokens if set (-1 = auto, > 0 = manual)
    if (currentThinkingBudget !== 0) {
        formData.append('thinking_budget_tokens', currentThinkingBudget);
    }

    if (options.pdfPageStart && options.pdfPageEnd) {
        formData.append('pdf_page_start', options.pdfPageStart);
        formData.append('pdf_page_end', options.pdfPageEnd);
        if (options.pdfRetryToken) {
            formData.append('pdf_retry_token', options.pdfRetryToken);
        }
    }

    // Multi-AI: append model IDs if active
    if (isMultiAiRequest) {
        // Block file attachments in Multi-AI v1
        if (outgoingFiles && outgoingFiles.length > 0) {
            NotificationModal.warning(
                'Multi-AI',
                'File attachments are not supported in Multi-AI Compare mode. Please disable Multi-AI or remove the attached files.'
            );
            if (userMessageElement) userMessageElement.remove();
            removeLoadingIndicator();
            document.getElementById('message-text').disabled = false;
            document.getElementById('message-text').focus();
            return false;
        }
        formData.append('multi_ai_models', JSON.stringify(selectedMultiAiModels.map((model) => model.llm_id)));
    }

    const filesForRetry = outgoingFiles;
    const hadOutgoingAttachments = filesForRetry.length > 0;
    const retryRangeBaseStart = Number.isInteger(parseInt(options.pdfPageStart, 10))
        ? parseInt(options.pdfPageStart, 10)
        : 1;
    const attachmentDisplayOptions = {
        pdfPageStart: options.pdfPageStart || null,
        pdfPageEnd: options.pdfPageEnd || null,
    };
    let renderedAttachmentElements = [];
    renderedAttachmentElements.cancelled = false;
    renderedAttachmentElements.attachmentRefs = [];

    // Hoisted for error cleanup access across the entire promise chain
    let botMessageElement = null;
    let botMessageParagraph = null;
    let streamContentReceived = false;
    let streamErrorOccurred = false;
    let persistenceErrorOccurred = false;
    let streamSucceeded = false;
    let pdfTooLargeError = null;
    let pdfRangeRetryStarted = false;
    let uploadedAttachmentRefs = [];
    userStopped = false;

    const chatPerfTraceEnabled = (() => {
        try {
            const params = new URLSearchParams(window.location.search);
            return localStorage.getItem('chatPerfTrace') === '1' || params.get('chatPerfTrace') === '1';
        } catch (error) {
            return false;
        }
    })();
    const chatPerfTrace = chatPerfTraceEnabled ? {
        traceId: `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
        startedAt: performance.now(),
        marks: [],
        firstStreamByteSeen: false,
        firstContentSeen: false,
        finished: false,
    } : null;

    function markChatPerf(name, details = {}) {
        if (!chatPerfTrace) return;
        const mark = {
            name,
            client_elapsed_ms: Math.round((performance.now() - chatPerfTrace.startedAt) * 10) / 10,
            ...details,
        };
        chatPerfTrace.marks.push(mark);
        console.debug('[chat-perf]', chatPerfTrace.traceId, mark);
    }

    function finishChatPerf(finalName = 'client_stream_done') {
        if (!chatPerfTrace || chatPerfTrace.finished) return;
        chatPerfTrace.finished = true;
        markChatPerf(finalName);
        console.table(chatPerfTrace.marks);
    }

    markChatPerf('client_send_start', {
        conversation_id: sendConversationId,
        message_chars: messageText_raw.length,
        attachment_count: outgoingFiles ? outgoingFiles.length : 0,
        multi_ai: isMultiAiRequest,
    });

    function discardUploadedRefs() {
        if (uploadedAttachmentRefs.length === 0) return;
        if (typeof discardUploadedAttachmentRefs === 'function') {
            discardUploadedAttachmentRefs(sendConversationId, uploadedAttachmentRefs);
        }
        uploadedAttachmentRefs = [];
    }

    function cleanupFailedStream(userMsgEl, botMsgEl, restoreText) {
        const hadAttachments = hadOutgoingAttachments;
        renderedAttachmentElements.cancelled = true;

        // Remove empty/partial bot bubble
        if (botMsgEl && botMsgEl.parentNode) {
            botMsgEl.remove();
        }

        // Handle user message cleanup
        if (hadAttachments) {
            // Files were sent -- keep user message in DOM for visual context
        } else if (userMsgEl && userMsgEl.parentNode) {
            if (restoreText) {
                const originalText = userMsgEl.querySelector('p')?.textContent;
                if (originalText) {
                    document.getElementById('message-text').value = originalText;
                }
            }
            userMsgEl.remove();
        }

        removeLoadingIndicator();
        toggleSendButton('Send');
        document.getElementById('message-text').disabled = false;
        const submitBtn = document.querySelector('#form-message button[type="submit"]');
        if (submitBtn) submitBtn.disabled = false;
        document.getElementById('message-text').focus();
        document.getElementById('message-text').style.height = 'auto';
    }

    function resetSendUiForActiveConversation() {
        removeLoadingIndicator();
        toggleSendButton('Send');
        const textInput = document.getElementById('message-text');
        const submitBtn = document.querySelector('#form-message button[type="submit"]');
        const canEnable = currentConversationId !== null && !isCurrentConversationLocked;
        if (textInput) {
            textInput.disabled = !canEnable;
            textInput.style.height = 'auto';
        }
        if (submitBtn) submitBtn.disabled = !canEnable;
    }

    function removeRetryEcho(restoreControls = currentConversationId === sendConversationId) {
        renderedAttachmentElements.cancelled = true;
        if (userMessageElement && userMessageElement.parentNode) {
            userMessageElement.remove();
        }
        renderedAttachmentElements.forEach((el) => {
            if (el && el.parentNode) {
                el.remove();
            }
        });
        if (botMessageElement && botMessageElement.parentNode) {
            botMessageElement.remove();
        }
        if (!restoreControls) {
            return;
        }
        removeLoadingIndicator();
        toggleSendButton('Send');
        const submitBtn = document.querySelector('#form-message button[type="submit"]');
        if (submitBtn) submitBtn.disabled = false;
        const textInput = document.getElementById('message-text');
        textInput.disabled = false;
        textInput.style.height = 'auto';
    }

    function showPdfRangeRetryModal(errorData) {
        if (currentConversationId !== sendConversationId) {
            removeRetryEcho(false);
            resetSendUiForActiveConversation();
            NotificationModal.warning(
                'PDF too large',
                'The PDF failed after you changed conversations. Return to the original conversation and resend a smaller page range.'
            );
            return;
        }

        removeLoadingIndicator();
        toggleSendButton('Send');
        const submitBtn = document.querySelector('#form-message button[type="submit"]');
        if (submitBtn) submitBtn.disabled = false;
        document.getElementById('message-text').disabled = false;

        if (errorData.range_retry_available === false) {
            removeRetryEcho();
            document.getElementById('message-text').value = messageText_raw;
            NotificationModal.warning(
                'PDF too large',
                'A PDF already in this conversation is too large for the selected AI model. Re-attach that PDF and send a smaller page range, or start a new conversation without it.'
            );
            return;
        }

        const retryPdfFiles = filesForRetry.filter((file) => file && file.type === 'application/pdf');
        if (retryPdfFiles.length !== 1) {
            removeRetryEcho();
            if (currentConversationId === sendConversationId) {
                document.getElementById('message-text').value = messageText_raw;
            }
            NotificationModal.warning(
                'PDF too large',
                'Page range retry supports one PDF attachment at a time. Re-attach one PDF and try again.'
            );
            return;
        }

        const pages = parseInt(errorData.retry_pages || errorData.pages || 0, 10);
        const maxPage = pages > 0 ? pages : 1000;
        const suggestedPageEnd = parseInt(errorData.suggested_page_end || 0, 10);
        const defaultEnd = Number.isInteger(suggestedPageEnd) && suggestedPageEnd > 0
            ? Math.min(maxPage, suggestedPageEnd)
            : Math.min(maxPage, 100);
        const filename = (errorData.retry_filename || errorData.filename)
            ? escapeHtml(errorData.retry_filename || errorData.filename)
            : 'document.pdf';
        const retryHint = errorData.retry_hint
            ? `<div class="pdf-range-provider-error">${escapeHtml(errorData.retry_hint)}</div>`
            : '';
        const contextPdfNote = parseInt(errorData.context_pdf_count || 0, 10) > 0
            ? '<p>Previous PDFs in this conversation will be ignored for this retry.</p>'
            : '';
        const html = `
            <div class="pdf-range-retry">
                <p>PDF too large for the selected AI model.</p>
                <p><strong>${filename}</strong>${pages ? ` has ${pages} pages.` : ''}</p>
                ${contextPdfNote}
                <div class="pdf-range-inputs">
                    <label for="pdf-range-start">From page</label>
                    <input id="pdf-range-start" class="form-control" type="number" min="1" max="${maxPage}" value="1">
                    <label for="pdf-range-end">To page</label>
                    <input id="pdf-range-end" class="form-control" type="number" min="1" max="${maxPage}" value="${defaultEnd}">
                </div>
                <div id="pdf-range-error" class="pdf-range-error" style="display:none;"></div>
                ${retryHint}
            </div>
        `;
        let rangeActionTaken = false;

        NotificationModal.confirm(
            'PDF too large',
            html,
            (modal) => {
                if (currentConversationId !== sendConversationId) {
                    rangeActionTaken = true;
                    modal.hide();
                    removeRetryEcho(false);
                    NotificationModal.warning(
                        'Conversation changed',
                        'Return to the original conversation and resend the PDF range from there.'
                    );
                    return;
                }
                const start = parseInt(document.getElementById('pdf-range-start')?.value || '', 10);
                const end = parseInt(document.getElementById('pdf-range-end')?.value || '', 10);
                const errorEl = document.getElementById('pdf-range-error');
                if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start || end > maxPage) {
                    if (errorEl) {
                        errorEl.textContent = `Enter a valid range between 1 and ${maxPage}.`;
                        errorEl.style.display = 'block';
                    }
                    return;
                }
                if ((end - start + 1) > 1000) {
                    if (errorEl) {
                        errorEl.textContent = 'Select at most 1000 pages.';
                        errorEl.style.display = 'block';
                    }
                    return;
                }
                rangeActionTaken = true;
                pdfRangeRetryStarted = true;
                modal.hide();
                removeRetryEcho();
                const absoluteStart = retryRangeBaseStart + start - 1;
                const absoluteEnd = retryRangeBaseStart + end - 1;
                sendMessage(messageText_raw, {
                    filesOverride: filesForRetry,
                    pdfPageStart: absoluteStart,
                    pdfPageEnd: absoluteEnd,
                    pdfRetryToken: errorData.retry_token || null
                });
            },
            () => {
                rangeActionTaken = true;
                removeRetryEcho();
                if (currentConversationId === sendConversationId) {
                    document.getElementById('message-text').value = messageText_raw;
                }
            },
            {
                type: 'warning',
                confirmText: 'Send range',
                cancelText: 'Do not send',
                allowHtml: true,
                hideOnConfirm: false
            }
        );

        const modalEl = NotificationModal._modalElement;
        if (modalEl) {
            modalEl.addEventListener('hidden.bs.modal', () => {
                if (rangeActionTaken || pdfRangeRetryStarted) return;
                rangeActionTaken = true;
                removeRetryEcho();
                if (currentConversationId === sendConversationId) {
                    document.getElementById('message-text').value = messageText_raw;
                }
            }, { once: true });
        }
    }

    controller = new AbortController();
    const signal = controller.signal;
    const attachmentTimeoutMessage = 'The request timed out while Aurvek was processing the message. Uploaded files were not linked to a saved message; please try again or choose a smaller PDF range.';

    toggleSendButton('Stop');

    Promise.resolve()
    .then(() => {
        if (!hadOutgoingAttachments) {
            return [];
        }
        if (typeof uploadAttachmentsForMessage !== 'function') {
            const error = new Error('Attachment upload is not available. Reload the page and try again.');
            error.uploadFailed = true;
            throw error;
        }
        return uploadAttachmentsForMessage(
            filesForRetry,
            sendConversationId,
            {
                ...attachmentDisplayOptions,
                signal,
            }
        );
    })
    .then((uploadedElements) => {
        if (uploadedElements && uploadedElements.length) {
            renderedAttachmentElements = uploadedElements;
            const refs = (uploadedElements.attachmentRefs || [])
                .map((attachment) => attachment.attachment_ref)
                .filter(Boolean);
            if (refs.length > 0) {
                uploadedAttachmentRefs = refs;
                formData.append('attachment_refs', JSON.stringify(refs));
            }
        }
        if (hadOutgoingAttachments && !formData.has('attachment_refs')) {
            const error = new Error('Attachment upload did not complete.');
            error.uploadFailed = true;
            throw error;
        }
        if (!options.filesOverride && hadOutgoingAttachments &&
            typeof window.removeAttachedFileBatch === 'function') {
            window.removeAttachedFileBatch(filesForRetry);
        }
        markChatPerf('client_fetch_start');
        const fetchHeaders = chatPerfTrace ? {
            'X-Chat-Trace': '1',
            'X-Chat-Trace-Id': chatPerfTrace.traceId,
            'X-Chat-Client-Sent-At': String(Date.now()),
        } : undefined;
        return secureFetch(`/api/conversations/${sendConversationId}/messages`, {
            method: 'POST',
            body: formData,
            signal: signal,
            headers: fetchHeaders,
        });
    })
    .then(response => {
        if (response) {
            markChatPerf('client_headers_received', { status: response.status });
        }

        const extractServerError = (resp) => {
            if (!resp) return Promise.resolve('Request failed');
            if (resp.status === 524) {
                return Promise.resolve(attachmentTimeoutMessage);
            }
            return resp.clone().json()
                .then((body) => {
                    if (!body || typeof body !== 'object') return `Request failed (${resp.status})`;
                    return body.error || body.message || body.detail || `Request failed (${resp.status})`;
                })
                .catch(() => {
                    return resp.text()
                        .then((txt) => {
                            const trimmed = typeof txt === 'string' ? txt.trim() : '';
                            return trimmed || `Request failed (${resp.status})`;
                        })
                        .catch(() => `Request failed (${resp.status})`);
                });
        };

        if (!response) {
            // secureFetch returned null (session expired)
            discardUploadedRefs();
            return null;
        }
        if (response.status === 401) {
            discardUploadedRefs();
            return response.json().then(data => {
                if (data.redirect) {
                    window.location.href = data.redirect;
                    return null;
                }
            });
        }
        if (response.status === 403) {
            discardUploadedRefs();
            cleanupFailedStream(userMessageElement, botMessageElement, true);
            document.getElementById('loading-indicator').style.display = 'none';

            // Try to parse response to distinguish app-level lock from external block (e.g. Cloudflare WAF)
            return response.clone().json().then(body => {
                const isAppLock = body.message && body.message.toLowerCase().includes('locked');
                if (isAppLock) {
                    // Conversation is locked (e.g. watchdog force-lock)
                    isCurrentConversationLocked = true;
                    const lockedBanner = document.getElementById('locked-conversation-banner');
                    if (lockedBanner) lockedBanner.style.display = 'flex';
                    const msgInput = document.getElementById('message-text');
                    msgInput.placeholder = 'This conversation is locked';
                    msgInput.disabled = true;
                    const submitBtn = document.querySelector('#form-message button[type="submit"]');
                    if (submitBtn) submitBtn.disabled = true;
                    refreshActiveConversation();
                } else {
                    // JSON 403 but not a lock
                    const msg = body?.error || body?.message || body?.detail || 'Request blocked';
                    NotificationModal.error('Message blocked', String(msg));
                }
                return null;
            }).catch(() => {
                // Non-JSON 403 = external block (Cloudflare WAF, firewall, etc.) — NOT a conversation lock
                NotificationModal.error('Message blocked', 'Request blocked by external security filter (403).');
                console.warn('Message blocked by external security filter (403). The conversation is NOT locked.');
                return null;
            });
        }

        if (!response.ok) {
            return response.clone().json()
                .then((body) => {
                    if (body && (body.error_code === 'pdf_too_large' || body.pdf_too_large === true)) {
                        pdfTooLargeError = body;
                        discardUploadedRefs();
                        showPdfRangeRetryModal(body);
                        return null;
                    }
                    if (body && (
                        body.error_code === 'conversation_model_changed' ||
                        body.error_code === 'expected_llm_id_required'
                    )) {
                        discardUploadedRefs();
                        removeRetryEcho();
                        if (conversationIdsMatch(
                            currentConversationId,
                            sendConversationId
                        )) {
                            document.getElementById('message-text').value = messageText_raw;
                        }
                        if (conversationIdsMatch(
                            currentConversationId,
                            sendConversationId
                        )) {
                            window.conversationModelIdentityUnknown = true;
                        }
                        if (window.modelSelector) {
                            window.modelSelector.invalidateCachedModel(sendConversationId);
                            void window.modelSelector.reconcileConversationModelIdentity(
                                sendConversationId
                            );
                        }
                        NotificationModal.warning(
                            'AI model changed',
                            (body.message || 'Review the selected AI model and send again.') +
                            (hadOutgoingAttachments
                                ? ' Re-attach the files before sending again.'
                                : '')
                        );
                        return null;
                    }
                    discardUploadedRefs();
                    cleanupFailedStream(userMessageElement, botMessageElement, true);
                    const msg = body?.error || body?.message || body?.detail || `Request failed (${response.status})`;
                    showProviderAwareError('Send failed', String(msg), body);
                    return null;
                })
                .catch(() => {
                    discardUploadedRefs();
                    cleanupFailedStream(userMessageElement, botMessageElement, true);
                    return extractServerError(response).then((msg) => {
                        showProviderAwareError('Send failed', String(msg));
                        return null;
                    });
                });
        }

        if (!response.body) {
            discardUploadedRefs();
            cleanupFailedStream(userMessageElement, botMessageElement, true);
            showProviderAwareError('Send failed', 'No response stream received from server.');
            return null;
        }

        return response.body.getReader();
    })
    .then(reader => {
        if (!reader) return; // If there was redirection, reader will be undefined

        const sseDecoder = new TextDecoder('utf-8');
        let sseBuffer = '';
        let abandonedPdfErrorHandled = false;

        function parseSseLines(value) {
            sseBuffer += sseDecoder.decode(value, { stream: true });
            const lines = sseBuffer.split('\n');
            sseBuffer = lines.pop();
            return lines;
        }

        function rememberPdfTooLargeError(parsedData) {
            if (parsedData && (parsedData.error_code === 'pdf_too_large' || parsedData.pdf_too_large === true)) {
                pdfTooLargeError = parsedData;
                streamErrorOccurred = true;
                return true;
            }
            return false;
        }

        function inspectAbandonedSseForPdfError(lines) {
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const data = line.slice(6).trim();
                if (!data || data === '[DONE]' || !data.startsWith('{')) continue;
                try {
                    const parsedData = JSON.parse(data);
                    if (rememberPdfTooLargeError(parsedData)) {
                        return true;
                    }
                } catch (err) {
                    // Ignore malformed partial events while draining; normal render path handles visible errors.
                }
            }
            return false;
        }

        function drainStreamSilently() {
            return reader.read().then(({ done, value }) => {
                if (done) {
                    if (pdfTooLargeError && !pdfRangeRetryStarted && !abandonedPdfErrorHandled) {
                        abandonedPdfErrorHandled = true;
                        showPdfRangeRetryModal(pdfTooLargeError);
                    }
                    return;
                }
                if (inspectAbandonedSseForPdfError(parseSseLines(value)) && !pdfRangeRetryStarted && !abandonedPdfErrorHandled) {
                    abandonedPdfErrorHandled = true;
                    showPdfRangeRetryModal(pdfTooLargeError);
                }
                return drainStreamSilently();
            }).catch(() => {});
        }

        function abandonStreamRender() {
            renderedAttachmentElements.cancelled = true;
            if (botMessageElement && botMessageElement.parentNode) {
                botMessageElement.remove();
            }
            resetSendUiForActiveConversation();
            return drainStreamSilently();
        }

        if (currentConversationId !== sendConversationId) {
            return abandonStreamRender();
        }

        let botMessageText = '';
        let endConversation = false;
        let newMessageId = null;
        let newUserMessageId = null;
        let updatedChatName = null;
        let streamCitations = null;

        // Create the bot message element before starting to read the stream
        botMessageElement = document.createElement('div');
        botMessageElement.classList.add('message', 'text-white', 'bot');
        botMessageElement.dataset.conversationId = sendConversationId;
        if (userMessageElement) {
            userMessageElement.dataset.conversationId = sendConversationId;
        }

        // Create the container for the avatar and the message
        const messageContentContainer = document.createElement('div');
        messageContentContainer.classList.add('message-content-container');

        // Create the avatar container
        const avatarContainer = createAvatar('bot');

        // Add avatar to message container
        messageContentContainer.appendChild(avatarContainer);

        // Create the div for the message content
        const messageContent = document.createElement('div');
        messageContent.classList.add('message-content');

        botMessageParagraph = null;
        let multiAiCarousel = null;
        if (isMultiAiRequest) {
            botMessageElement.classList.add('multi-ai-message');
            multiAiCarousel = createMultiAiCarousel(selectedMultiAiModels);
            botMessageElement._multiAiCarousel = multiAiCarousel;
            messageContent.appendChild(multiAiCarousel);
        } else {
            botMessageParagraph = document.createElement('p');
            messageContent.appendChild(botMessageParagraph);
        }

        // Add message content to message container
        messageContentContainer.appendChild(messageContent);

        // Add message container to main message div
        botMessageElement.appendChild(messageContentContainer);

        document.getElementById('chat-messages-container').appendChild(botMessageElement);

        // Add icons to bot message
        const infoContainer = document.createElement('div');
        infoContainer.classList.add('message-info');
        const iconContainer = document.createElement('div');
        iconContainer.classList.add('icon-container');

        const getCurrentBotText = () => {
            if (isMultiAiRequest) {
                const activeText = getActiveMultiAiSlideText(botMessageElement);
                return activeText || botMessageText;
            }
            return botMessageText;
        };

        const audioIcon = createIcon('fa-volume-up', 'inline', () => textToSpeech(getCurrentBotText(), user_id, sendConversationId, audioIcon, 'bot'));
        audioIcon.dataset.baseIcon = 'fa-volume-up';
        audioIcon.title = 'Read aloud';
        const bookmarkIcon = createIcon('fa-bookmark', 'none', function() {
            const messageElement = this.closest('.message');
            const messageId = messageElement ? messageElement.dataset.messageId : null;
            if (messageId) {
                toggleBookmark(messageId, sendConversationId, this);
            } else {
                console.error('Could not find message ID to mark as favorite');
            }
        });
        bookmarkIcon.title = 'Add to bookmarks';
        const copyIcon = createIcon('fa-copy', 'none', () => copyToClipboard(getCurrentBotText(), copyIcon));
        copyIcon.title = 'Copy text';
        const branchIcon = createIcon('fa-code-branch', 'none', () => branchConversation(newMessageId, sendConversationId));
        branchIcon.title = 'Branch from here';
        const rollbackIcon = createIcon('fa-level-up-alt', 'none', () => rollbackConversation(newMessageId, sendConversationId));
        rollbackIcon.title = 'Start over from here';

        iconContainer.appendChild(audioIcon);
        iconContainer.appendChild(bookmarkIcon);
        iconContainer.appendChild(rollbackIcon);
        iconContainer.appendChild(copyIcon);
        iconContainer.appendChild(branchIcon);

        const timeSpan = document.createElement('span');
        infoContainer.appendChild(iconContainer);
        infoContainer.appendChild(timeSpan);
        messageContent.appendChild(infoContainer);

        function createIcon(iconClass, styleDisplay, onClickFunction, messageId = null) {
            const icon = document.createElement('i');
            icon.classList.add('fas', iconClass);
            icon.style.cursor = 'pointer';
            icon.style.display = styleDisplay;
            if (messageId && (iconClass === 'fa-level-up-alt' || iconClass === 'fa-code-branch')) {
                icon.setAttribute('data-message-id', messageId);
                if (iconClass === 'fa-level-up-alt') {
                    icon.onclick = function() {
                        rollbackConversation(this.getAttribute('data-message-id'), sendConversationId);
                    };
                } else {
                    icon.onclick = function() {
                        branchConversation(this.getAttribute('data-message-id'), sendConversationId);
                    };
                }
            } else {
                icon.onclick = onClickFunction;
            }
            return icon;
        }

        removeLoadingIndicator();
        // Reset text field to original size
        document.getElementById('message-text').style.height = 'auto';
        addLoadingIndicator(multiAiLoadingText);
        scrollToBottomIfNeeded();

        function readStream() {
            if (currentConversationId !== sendConversationId) {
                return abandonStreamRender();
            }
            return reader.read().then(({ done, value }) => {
                if (currentConversationId !== sendConversationId) {
                    return abandonStreamRender();
                }
                if (done) {
                    finishChatPerf();
                    refreshMemoryHealthBanner();
                    if (userStopped && !streamContentReceived) {
                        if (botMessageElement?.parentNode) botMessageElement.remove();
                        toggleSendButton('Send');
                        return;
                    }
                    if (pdfTooLargeError && !pdfRangeRetryStarted) {
                        showPdfRangeRetryModal(pdfTooLargeError);
                        return;
                    }
                    if (persistenceErrorOccurred) {
                        toggleSendButton('Send');
                        initializeNewImages(botMessageElement);
                        return;
                    }
                    if (!streamContentReceived && !streamErrorOccurred) {
                        cleanupFailedStream(userMessageElement, botMessageElement, true);
                        const hadAttachments = hadOutgoingAttachments;
                        if (hadAttachments) {
                            NotificationModal.error('Empty response',
                                'The AI returned an empty response. Files were uploaded but the response failed. Please re-attach your files and try again.');
                        } else {
                            NotificationModal.error('Empty response',
                                'The AI returned an empty response. Your message has been restored. Please try again.');
                        }
                        return;
                    }
                    if (streamErrorOccurred && !streamContentReceived) {
                        cleanupFailedStream(userMessageElement, botMessageElement, true);
                        return;
                    }
                    streamSucceeded = true;
                    toggleSendButton('Send');
                    initializeNewImages(botMessageElement);
                    if (updatedChatName) {
                        updateActiveChatName(updatedChatName);
                    }
                    return;
                }
                if (chatPerfTrace && !chatPerfTrace.firstStreamByteSeen) {
                    chatPerfTrace.firstStreamByteSeen = true;
                    markChatPerf('client_first_stream_byte', { bytes: value ? value.byteLength : 0 });
                }
                const lines = parseSseLines(value);
        
                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const data = line.slice(6).trim();

                        if (data === '[DONE]') {
                            finishChatPerf();
                            refreshMemoryHealthBanner();
                            if (userStopped && !streamContentReceived) {
                                if (botMessageElement?.parentNode) botMessageElement.remove();
                                toggleSendButton('Send');
                                return;
                            }
                            if (pdfTooLargeError && !pdfRangeRetryStarted) {
                                showPdfRangeRetryModal(pdfTooLargeError);
                                return;
                            }
                            if (persistenceErrorOccurred) {
                                toggleSendButton('Send');
                                initializeNewImages(botMessageElement);
                                return;
                            }
                            if (!streamContentReceived && !streamErrorOccurred) {
                                cleanupFailedStream(userMessageElement, botMessageElement, true);
                                const hadAttachments = hadOutgoingAttachments;
                                if (hadAttachments) {
                                    NotificationModal.error('Empty response',
                                        'The AI returned an empty response. Files were uploaded but the response failed. Please re-attach your files and try again.');
                                } else {
                                    NotificationModal.error('Empty response',
                                        'The AI returned an empty response. Your message has been restored. Please try again.');
                                }
                                return;
                            }
                            if (streamErrorOccurred && !streamContentReceived) {
                                cleanupFailedStream(userMessageElement, botMessageElement, true);
                                return;
                            }
                            streamSucceeded = true;
                            toggleSendButton('Send');
                            initializeNewImages(botMessageElement);
                            return;
                        }
                        try {
                            const parsedData = JSON.parse(data);

                            if (parsedData.type === 'perf_trace') {
                                markChatPerf(`server:${parsedData.name}`, {
                                    server_elapsed_ms: parsedData.elapsed_ms,
                                    trace_id: parsedData.trace_id,
                                    machine: parsedData.machine,
                                    model: parsedData.model,
                                    input_tokens: parsedData.input_tokens,
                                    output_tokens: parsedData.output_tokens,
                                    provider_tool_count: parsedData.provider_tool_count,
                                });
                                continue;
                            }
                            if (parsedData.type === 'memory_health') {
                                setCurrentMemoryHealth(parsedData.memory_health || null);
                                continue;
                            }

                            // Handle actions from AI welfare system
                            if (parsedData.action === 'end_conversation') {
                                endConversation = true;
                                isCurrentConversationLocked = true;
                                // Update UI to show locked state
                                const selectedChat = document.querySelector(`.list-group-item[data-conversation-id="${sendConversationId}"]`);
                                if (selectedChat) {
                                    selectedChat.dataset.locked = 'true';
                                    selectedChat.classList.add('conversation-locked');
                                    // Update icon in sidebar
                                    const nameSpan = selectedChat.querySelector('.chat-name');
                                    if (nameSpan && !nameSpan.querySelector('.fa-comment-slash')) {
                                        const chatText = nameSpan.textContent;
                                        nameSpan.innerHTML = `<i class="fas fa-comment-slash" title="This conversation is locked"></i> ${chatText}`;
                                    }
                                }
                                // Show locked banner
                                if (currentConversationId === sendConversationId) {
                                    const lockedBanner = document.getElementById('locked-conversation-banner');
                                    if (lockedBanner) lockedBanner.style.display = 'flex';
                                }
                            }
                            // Note: pass_turn action sends '🚩' as content, which displays
                            // as a normal bot message. No special handling needed.

                            // Handle thinking tokens streaming
                            if (parsedData.type === 'thinking_start') {
                                if (messageContent && botMessageParagraph) {
                                    const existing = messageContent.querySelector('.thinking-block');
                                    if (existing) {
                                        // Reuse existing block: reopen and add separator
                                        existing.open = true;
                                        const summary = existing.querySelector('summary');
                                        if (summary) summary.textContent = 'Thinking...';
                                        const content = existing.querySelector('.thinking-content');
                                        if (content) content.textContent += '\n\n---\n\n';
                                    } else {
                                        const details = document.createElement('details');
                                        details.className = 'thinking-block';
                                        details.open = true;
                                        const summary = document.createElement('summary');
                                        summary.textContent = 'Thinking...';
                                        const content = document.createElement('pre');
                                        content.className = 'thinking-content';
                                        details.appendChild(summary);
                                        details.appendChild(content);
                                        messageContent.insertBefore(details, botMessageParagraph);
                                    }
                                }
                            } else if (parsedData.type === 'thinking') {
                                const thinkingContent = messageContent?.querySelector('.thinking-content');
                                if (thinkingContent && parsedData.thinking) {
                                    thinkingContent.textContent += parsedData.thinking;
                                    thinkingContent.scrollTop = thinkingContent.scrollHeight;
                                    scrollToBottomIfNeeded();
                                }
                            } else if (parsedData.type === 'thinking_end') {
                                const thinkingBlock = messageContent?.querySelector('.thinking-block');
                                if (thinkingBlock) {
                                    thinkingBlock.open = false;
                                    const summary = thinkingBlock.querySelector('summary');
                                    if (summary) summary.textContent = 'Thought process (not saved)';
                                }
                            } else if (parsedData.updated_chat_name) {
                                updateActiveChatName(parsedData.updated_chat_name);
                            } else if (parsedData.multi_ai && multiAiCarousel?._multiAiApi) {
                                streamContentReceived = true;
                                multiAiCarousel._multiAiApi.appendChunk(parsedData.llm_id, parsedData.content || '');
                                botMessageText = getActiveMultiAiSlideText(botMessageElement);
                                scrollToBottomIfNeeded();
                            } else if (parsedData.multi_ai_done && multiAiCarousel?._multiAiApi) {
                                multiAiCarousel._multiAiApi.markSlideDone(parsedData.llm_id);
                            } else if (parsedData.multi_ai_error && multiAiCarousel?._multiAiApi) {
                                if (parsedData.provider_health) {
                                    setCurrentProviderHealth(parsedData.provider_health);
                                }
                                multiAiCarousel._multiAiApi.setSlideError(parsedData.llm_id, parsedData.error || 'Unknown error');
                                scrollToBottomIfNeeded();
                            } else if (parsedData.persistence_error === true) {
                                const warningText = parsedData.error || 'The response was generated but could not be saved. Copy it before retrying.';
                                console.error('Response persistence error:', warningText);
                                streamErrorOccurred = true;
                                persistenceErrorOccurred = true;
                                copyIcon.style.display = 'inline';
                                if (parsedData.provider_health) {
                                    setCurrentProviderHealth(parsedData.provider_health);
                                }
                                if (multiAiCarousel?._multiAiApi) {
                                    multiAiCarousel._multiAiApi.setGlobalError(warningText);
                                } else if (botMessageParagraph && !botMessageParagraph.querySelector('.message-persistence-warning')) {
                                    const warningEl = document.createElement('span');
                                    warningEl.className = 'message-persistence-warning text-warning d-block mt-2';
                                    warningEl.setAttribute('role', 'alert');
                                    warningEl.textContent = warningText;
                                    botMessageParagraph.appendChild(warningEl);
                                }
                                NotificationModal.warning('Response not saved', warningText);
                                scrollToBottomIfNeeded();
	                            } else if (parsedData.error && !parsedData.multi_ai_error) {
	                                console.error('SSE error:', parsedData.error);
	                                streamErrorOccurred = true;
	                                if (parsedData.error_code === 'pdf_too_large' || parsedData.pdf_too_large === true) {
	                                    pdfTooLargeError = parsedData;
	                                    if (botMessageParagraph) {
	                                        botMessageParagraph.textContent = parsedData.error;
	                                    }
	                                    continue;
	                                }
                                    if (parsedData.provider_health) {
                                        setCurrentProviderHealth(parsedData.provider_health);
                                    }
	                                showProviderAwareError('AI Error', parsedData.error, parsedData);
	                                if (multiAiCarousel?._multiAiApi) {
                                    multiAiCarousel._multiAiApi.setGlobalError(parsedData.error);
                                } else if (botMessageParagraph) {
                                    botMessageParagraph.innerHTML = '';
                                    const errorEl = document.createElement('span');
                                    errorEl.classList.add('multi-ai-slide-error');
                                    errorEl.textContent = parsedData.error;
                                    botMessageParagraph.appendChild(errorEl);
                                }
                            } else if (parsedData.video_content && botMessageParagraph) {
                                streamContentReceived = true;
                                // Handle video content - render as video element
                                try {
                                    const videoData = JSON.parse(parsedData.video_content);
                                    if (videoData[0]?.type === 'video_url') {
                                        const videoObj = videoData[0].video_url;
                                        botMessageParagraph.innerHTML = '';
                                        const videoElement = document.createElement('video');
                                        videoElement.src = videoObj.url;
                                        videoElement.controls = true;
                                        videoElement.style.maxWidth = '100%';
                                        videoElement.style.maxHeight = '480px';
                                        videoElement.style.width = 'auto';
                                        videoElement.style.height = 'auto';
                                        videoElement.preload = 'metadata';
                                        if (videoObj.alt) {
                                            videoElement.setAttribute('aria-label', videoObj.alt);
                                            videoElement.title = videoObj.alt;
                                        }
                                        botMessageParagraph.appendChild(videoElement);
                                        botMessageText = '';
                                    }
                                } catch (e) {
                                    console.error('Error parsing video content:', e);
                                }
                                scrollToBottomIfNeeded();
                            } else if (parsedData.searching === true && botMessageParagraph) {
                                // Show pulsing "Searching the web..." indicator while Perplexity searches
                                const indicator = document.createElement('span');
                                indicator.className = 'searching-indicator';
                                indicator.innerHTML = '<i class="fas fa-search"></i> Searching the web...';
                                botMessageParagraph.innerHTML = '';
                                botMessageParagraph.appendChild(indicator);
                                scrollToBottomIfNeeded();
                            } else if (parsedData.searching === false && botMessageParagraph) {
                                // Remove searching indicator so content streaming starts clean
                                const indicator = botMessageParagraph.querySelector('.searching-indicator');
                                if (indicator) indicator.remove();
                                botMessageText = '';
                            } else if (parsedData.gransabio_verbose && botMessageParagraph) {
                                // GranSabio pipeline progress
                                renderGranSabioStatus(botMessageParagraph, parsedData.gransabio_verbose);
                                scrollToBottomIfNeeded();
                            } else if (parsedData.gransabio_complete && botMessageParagraph) {
                                // GranSabio pipeline complete summary
                                finalizeGranSabioStatus(botMessageParagraph, parsedData.gransabio_complete);
                                scrollToBottomIfNeeded();
                            } else if (parsedData.content && !parsedData.multi_ai && botMessageParagraph) {
                                if (chatPerfTrace && !chatPerfTrace.firstContentSeen) {
                                    chatPerfTrace.firstContentSeen = true;
                                    markChatPerf('client_first_content');
                                }
                                streamContentReceived = true;
                                // Handle replace_last for progress updates
                                if (parsedData.replace_last) {
                                    botMessageText = parsedData.content;
                                } else {
                                    botMessageText += parsedData.content;
                                }

                                renderMarkdownIntoElement(botMessageParagraph, botMessageText);
                                initializeNewImages(botMessageElement);
                                scrollToBottomIfNeeded();
                            } else if (parsedData.message_ids) {
								if (parsedData.message_ids.bot) {
									newMessageId = parsedData.message_ids.bot;
								}
                                if (parsedData.message_ids.user) {
                                    newUserMessageId = parsedData.message_ids.user;
                                }
							} else if (parsedData.type === 'web_search_citations') {
                                streamCitations = parsedData.citations || [];
                            }

                            // Handle extension level change from server
                            if (parsedData.extension_changed && window.extensionSelector) {
                                window.extensionSelector.updateFromSSE(
                                    parsedData.extension_changed,
                                    sendConversationId
                                );
                            }

                        } catch (error) {
                            console.error('Error parsing JSON:', error);
                        }
                    }
                }
                return readStream();
            });
        }

        // Add timestamp and icons to bot message
        let botTimestamp = new Date();
        let localBotTimestamp = botTimestamp.toLocaleString(undefined, {
            year: 'numeric',
            month: 'numeric',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
            timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone
        });
        
        timeSpan.textContent = botTimestamp.toLocaleString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            hour12: false
        });
        timeSpan.title = localBotTimestamp;

        return readStream().then(() => {
            removeLoadingIndicator();
            document.getElementById('message-text').disabled = false;
            const submitBtnEl = document.querySelector('#form-message button[type="submit"]');
            if (submitBtnEl) submitBtnEl.disabled = false;
            document.getElementById('message-text').focus();
            scrollToBottomIfNeeded();

            if (streamSucceeded) {
                isCurrentConversationEmpty = false;
                if (endConversation) {
                    document.getElementById('message-text').disabled = true;
                    document.getElementById('send-button').disabled = true;
                    toggleSendButton('Send');
                    document.getElementById('send-button').onclick = null;
                } else {
                    toggleSendButton('Send');
                }

                // Update the bot message ID
                if (newMessageId) {
                    updateMessageId(newMessageId, botMessageElement);
                }
                if (newUserMessageId && userMessageElement) {
                    updateMessageId(newUserMessageId, userMessageElement);
                }

                applyCollapsibleCodeBlocks(botMessageElement);

                // Render web search citations if any were collected
                if (streamCitations && streamCitations.length > 0) {
                    const sourcesBlock = buildSourcesBlock(streamCitations);
                    if (sourcesBlock) {
                        const msgContent = botMessageElement.querySelector('.message-content');
                        const msgInfo = msgContent.querySelector('.message-info');
                        msgContent.insertBefore(sourcesBlock, msgInfo);
                    }
                }

                // Multi-AI: reset state after message completes
                if (window.multiAiManager) {
                    window.multiAiManager.afterMessageSent();
                }

                // Move this conversation to the top of the sidebar
                moveConversationToTop(sendConversationId);

                // Add event listeners to show/hide icons
                botMessageElement.addEventListener('mouseover', function() {
                    const messageId = this.dataset.messageId;
                    this.querySelectorAll('.fa-volume-up, .fa-bookmark, .fa-copy, .fa-code-branch, .fa-level-up-alt').forEach(icon => {
                        if (!icon.classList.contains('bookmarked')) {
                            if (icon.classList.contains('fa-bookmark') && !messageId) {
                                icon.style.display = 'none';
                                return;
                            }
                            icon.style.display = 'inline';
                        }
                    });
                });

                botMessageElement.addEventListener('mouseout', function() {
                    this.querySelectorAll('.fa-volume-up, .fa-bookmark, .fa-copy, .fa-code-branch, .fa-level-up-alt').forEach(icon => {
                        if (!icon.classList.contains('bookmarked')) {
                            icon.style.display = 'none';
                        }
                    });
                });
            }
        });
    })
    .catch(error => {
        markChatPerf('client_error', { error: error && error.message ? error.message : String(error) });
        finishChatPerf('client_stream_error');
        if (error.uploadFailed) {
            if (error.renderedAttachmentElements) {
                renderedAttachmentElements = error.renderedAttachmentElements;
                uploadedAttachmentRefs = typeof getAttachmentRefsFromUploadElements === 'function'
                    ? getAttachmentRefsFromUploadElements(renderedAttachmentElements)
                    : uploadedAttachmentRefs;
            }
            discardUploadedRefs();
            // Remove the failed attachment echoes and revoke their blob object URLs
            // so a re-send does not stack "Failed" echoes or leak blob URLs.
            if (error.renderedAttachmentElements) {
                error.renderedAttachmentElements.forEach((el) => {
                    if (!el) return;
                    el.querySelectorAll('img[src^="blob:"]').forEach((img) => {
                        URL.revokeObjectURL(img.src);
                    });
                    if (el.parentNode) {
                        el.remove();
                    }
                });
            }
            if (botMessageElement && botMessageElement.parentNode) {
                botMessageElement.remove();
            }
            if (userMessageElement && userMessageElement.parentNode) {
                document.getElementById('message-text').value = messageText_raw;
                userMessageElement.remove();
            }
            removeLoadingIndicator();
            toggleSendButton('Send');
            document.getElementById('message-text').disabled = false;
            const submitBtn = document.querySelector('#form-message button[type="submit"]');
            if (submitBtn) submitBtn.disabled = false;
            document.getElementById('message-text').focus();
            document.getElementById('message-text').style.height = 'auto';
            NotificationModal.error('Upload failed', error.message || 'Attachment upload failed before the message was sent.');
            return;
        }

        if (error.name === 'AbortError') {
            discardUploadedRefs();
            // Clean up in-progress attachment echoes (and revoke their blob URLs)
            // from the cancelled upload, mirroring the uploadFailed branch.
            if (error.renderedAttachmentElements) {
                error.renderedAttachmentElements.forEach((el) => {
                    if (!el) return;
                    el.querySelectorAll('img[src^="blob:"]').forEach((img) => {
                        URL.revokeObjectURL(img.src);
                    });
                    if (el.parentNode) {
                        el.remove();
                    }
                });
            }
            removeLoadingIndicator();
            toggleSendButton('Send');
            document.getElementById('message-text').disabled = false;
            const submitBtn = document.querySelector('#form-message button[type="submit"]');
            if (submitBtn) submitBtn.disabled = false;
            document.getElementById('message-text').focus();
            return;
        }
        console.error('Stream error:', error);

        if (streamContentReceived) {
            // Partial content received -- backend likely saved. Keep messages.
            removeLoadingIndicator();
            toggleSendButton('Send');
            document.getElementById('message-text').disabled = false;
            const submitBtn = document.querySelector('#form-message button[type="submit"]');
            if (submitBtn) submitBtn.disabled = false;
            document.getElementById('message-text').focus();
            document.getElementById('message-text').style.height = 'auto';
            NotificationModal.warning('Stream interrupted',
                'The response was interrupted. Partial content may have been saved. Reload to verify.');
        } else {
            discardUploadedRefs();
            cleanupFailedStream(userMessageElement, botMessageElement, true);
            const hadAttachments = hadOutgoingAttachments;
            if (hadAttachments) {
                showProviderAwareError('Message failed',
                    attachmentTimeoutMessage);
            } else {
                showProviderAwareError('Message failed',
                    'The message could not be sent. Your text has been restored. Please try again.');
            }
        }
    });

    return true;
}

function updateMessageId(messageId, element) {
    if (element) {
        const isBotMessage = element.classList.contains('bot');
        const targetConversationId = element.dataset.conversationId || currentConversationId;
        element.dataset.messageId = messageId;
        const rollbackIcon = element.querySelector('.fa-level-up-alt');
        if (rollbackIcon) {
            rollbackIcon.setAttribute('data-message-id', messageId);
            rollbackIcon.style.display = 'inline';
            rollbackIcon.onclick = function() {
                rollbackConversation(messageId, targetConversationId);
            };
        } else if (isBotMessage) {
        }
        const branchIcon = element.querySelector('.fa-code-branch');
        if (branchIcon) {
            branchIcon.setAttribute('data-message-id', messageId);
            branchIcon.onclick = function() {
                branchConversation(messageId, targetConversationId);
            };
        }
    } else {
    }
}

function getLastMessageId(element = null) {
    return fetch(`/api/conversations/${currentConversationId}/last_message_id`)
        .then(response => response.json())
        .then(data => {
            if (data.message_id) {
                if (element) {
                    updateMessageId(data.message_id, element);
                }
                return data.message_id;
            } else {
                return null;
            }
        })
        .catch(error => {
            console.error('Error fetching last message ID:', error);
            return null;
        });
}

function createAvatar(author) {
    const avatarContainer = document.createElement('div');
    avatarContainer.classList.add('avatar');

    if (author === 'user') {
        if (userProfilePicture) {
            var avatarImg = document.createElement('img');
            avatarImg.src = userProfilePicture;
            avatarImg.alt = username;
            avatarImg.title = username;
            avatarImg.classList.add('avatar-img');
            avatarContainer.appendChild(avatarImg);
        } else {
            var userInitial = username.charAt(0).toUpperCase();
            avatarContainer.textContent = userInitial;
            avatarContainer.title = username;
        }
    } else {
        if (botProfilePicture) {
            var avatarImg = document.createElement('img');
            avatarImg.src = botProfilePicture;
            avatarImg.alt = botname;
            avatarImg.title = botname;
            avatarImg.classList.add('avatar-img');
            avatarContainer.appendChild(avatarImg);
        } else {
            var botInitial = (botname && botname.length > 0) ? botname.charAt(0).toUpperCase() : 'B';
            avatarContainer.textContent = botInitial;
            avatarContainer.title = botname || 'Bot';
        }
    }

    return avatarContainer;
}

function updateActiveChatName(newName) {
    
    // Search for the active chat anywhere (main list or folders)
    const activeChatElement = document.querySelector('.active-chat');
    
    if (activeChatElement) {
        const chatNameSpan = activeChatElement.querySelector('.chat-name');

        if (chatNameSpan) {
            // Preserve icon (WhatsApp, locked) when updating name
            const icon = chatNameSpan.querySelector('i');
            chatNameSpan.textContent = '';
            if (icon) {
                chatNameSpan.appendChild(icon);
                chatNameSpan.appendChild(document.createTextNode(` ${newName}`));
            } else {
                chatNameSpan.textContent = newName;
            }
        } else {
            console.error('Element span.chat-name not found within active chat');
        }
    } else {
        console.error('Active chat element not found');
    }

    // Update chat title at the top
    const chatTitle = document.querySelector('.chatbot-info h4');
    if (chatTitle) {
        const dateOptions = { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' };
        const formattedStartDate = new Date(startDate).toLocaleDateString(undefined, dateOptions);
        chatTitle.textContent = `${newName}`;
        chatTitle.title = `Created: ${formattedStartDate}`;
    }
}

const loadedConversationIds = new Set();

function addConversationElement(conversation, chatName, currentConversationId, isNew = false) {
    //console.log(`Adding conversation: ${conversation.id} External: ${conversation.external_platform} Chat Name: ${chatName}`);
    if (conversation.hidden_from_history || conversation.is_incognito) {
        return;
    }
    
    // Ensure we always have a valid name
    chatName = chatName || `Chat ${conversation.id}`;

    // Check if element already exists for this conversation
    const existingElement = document.querySelector(`[data-conversation-id="${conversation.id}"]`);
    if (existingElement) {
        // Preserve external state if it already existed
        if (!conversation.external_platform && existingElement.dataset.externalPlatform) {
            conversation.external_platform = existingElement.dataset.externalPlatform;
        }
        updateSingleConversation(existingElement, conversation, document.querySelector('#external-chats-container'), document.querySelector('#dynamic-chats-container'));
        return;
    }

    if (loadedConversationIds.has(conversation.id)) {
        return; // Skip if already loaded
    }
    
    loadedConversationIds.add(conversation.id);

    const dynamicChatsContainer = document.querySelector('#dynamic-chats-container');
    const externalChatsContainer = document.querySelector('#external-chats-container');
    const conversationElement = document.createElement('a');
    conversationElement.href = '#';
    conversationElement.classList.add('list-group-item', 'list-group-item-action');
    conversationElement.dataset.conversationId = conversation.id;
    conversationElement.dataset.machine = conversation.machine;
    conversationElement.dataset.llmModel = conversation.llm_model || '';
    if (conversation.llm_id) {
        conversationElement.dataset.llmId = String(conversation.llm_id);
    }
    if (conversation.prompt_id) {
        conversationElement.dataset.promptId = String(conversation.prompt_id);
    }
    conversationElement.dataset.locked = conversation.locked ? 'true' : 'false';
    conversationElement.dataset.webSearchAllowed = conversation.web_search_allowed !== false ? 'true' : 'false';
    conversationElement.dataset.webSearchForced = conversation.web_search_forced === true ? 'true' : 'false';
    conversationElement.dataset.isIncognito = conversation.is_incognito ? 'true' : 'false';
    if (conversation.forced_llm_id) {
        conversationElement.dataset.forcedLlmId = conversation.forced_llm_id;
    }
    if (conversation.hide_llm_name) {
        conversationElement.dataset.hideLlmName = 'true';
    }
    if (conversation.allowed_llms) {
        conversationElement.dataset.allowedLlms = JSON.stringify(conversation.allowed_llms);
    }
    conversationElement.dataset.isPaid = conversation.is_paid ? '1' : '0';
    conversationElement.dataset.lastActivity = conversation.last_activity || '';
    if (conversation.locked) {
        conversationElement.classList.add('conversation-locked');
    }
    if (conversation.external_platform) {
        conversationElement.dataset.externalPlatform = conversation.external_platform;
    }
    if (conversation.id === currentConversationId) {
        conversationElement.classList.add('active-chat');
    }
    conversationElement._conversationData = conversation;

    // Create conversation element content
    const nameSpan = document.createElement('span');
    nameSpan.className = 'chat-name';
    if (conversation.external_platform) {
        const iconClass = getExternalPlatformIcon(conversation.external_platform);
        nameSpan.innerHTML = `<i class="${iconClass}"></i> ${chatName}`;
    } else if (conversation.locked) {
        nameSpan.innerHTML = `<i class="fas fa-comment-slash" title="This conversation is locked"></i> ${chatName}`;
    } else {
        nameSpan.textContent = chatName;
    }
    conversationElement.appendChild(nameSpan);

    const externalDeviceBadge = createExternalDeviceBadge(conversation);
    if (externalDeviceBadge) {
        conversationElement.classList.add('external-device-row');
        conversationElement.appendChild(externalDeviceBadge);
    }

    // Create and add menu
    const chatMenu = createChatMenu(conversation);
    conversationElement.appendChild(chatMenu);

    // Handle conversation addition
    if (conversation.external_platform) {
        // Remove any existing entry for the same platform to avoid duplicates
        const existing = externalChatsContainer.querySelector(`[data-external-platform="${conversation.external_platform}"]`);
        if (existing) {
            existing.remove();
        }

        // Add conversation to external section
        externalChatsContainer.appendChild(conversationElement);
        document.querySelector('.external-section').style.display = 'block';
    } else {
        if (isNew) {
            dynamicChatsContainer.insertBefore(conversationElement, dynamicChatsContainer.firstChild);
        } else {
            dynamicChatsContainer.appendChild(conversationElement);
        }
    }
    setupConversationElementListeners(conversationElement);
}

// Close all open chat context menus and return portaled menus to their original parents
function closeAllChatMenus() {
    document.querySelectorAll('.chat-menu-content').forEach(menu => {
        if (menu.style.display !== 'block' && !menu._originParent) return;
        menu.style.display = 'none';
        menu.classList.remove('menu-above');
        if (menu._originParent) {
            const parentItem = menu._originParent.closest('.list-group-item');
            if (parentItem) parentItem.style.zIndex = '';
            menu._originParent.appendChild(menu);
            menu.style.position = '';
            menu.style.top = '';
            menu.style.right = '';
            menu.style.left = '';
            menu.style.zIndex = '';
            menu._originParent = null;
        } else {
            const parentItem = menu.closest('.list-group-item');
            if (parentItem) parentItem.style.zIndex = '';
        }
    });
}

// One delegated outside-click listener covers every current and future menu.
document.addEventListener('click', (event) => {
    if (!event.target.closest?.('.chat-menu, .chat-menu-content')) {
        closeAllChatMenus();
    }
});

function createChatMenu(conversation) {
    const chatMenu = document.createElement('div');
    chatMenu.classList.add('chat-menu');

    const ellipsisIcon = document.createElement('i');
    ellipsisIcon.classList.add('fas', 'fa-ellipsis-h');
    chatMenu.appendChild(ellipsisIcon);

    const chatMenuContent = document.createElement('div');
    chatMenuContent.classList.add('chat-menu-content');
    chatMenu.appendChild(chatMenuContent);

    // Rename option
    const renameLink = createMenuLink('fa-edit', 'Rename', () => renameConversation(conversation.id));
    chatMenuContent.appendChild(renameLink);

    // Download as MP3 option
    const downloadAudioLink = createMenuLink('fa-music', 'Download MP3', () => downloadAudio(conversation.id));
    chatMenuContent.appendChild(downloadAudioLink);

    // Download as PDF option
    const downloadPdfLink = createMenuLink('fa-download', 'Download PDF', () => downloadPDF(conversation.id));
    chatMenuContent.appendChild(downloadPdfLink);

    // Delete option
    const deleteLink = createMenuLink('fa-trash-alt', 'Delete', () => deleteConversation(conversation.id), 'text-danger');
    chatMenuContent.appendChild(deleteLink);

    // Lock/Unlock option (admin only)
    if (typeof isAdmin !== 'undefined' && isAdmin) {
        const isLocked = conversation.locked;
        const lockIcon = isLocked ? 'fa-lock-open' : 'fa-lock';
        const lockText = isLocked ? 'Unlock' : 'Lock';
        const lockLink = createMenuLink(lockIcon, lockText, () => toggleLockConversation(conversation.id, !isLocked));
        chatMenuContent.appendChild(lockLink);
    }

    // Add separator
    const separator = document.createElement('div');
    separator.classList.add('menu-separator');
    chatMenuContent.appendChild(separator);

    // WhatsApp option
    const whatsappLink = createPlatformLink('whatsapp', conversation);
    chatMenuContent.appendChild(whatsappLink);

    // Telegram option
    const telegramLink = createPlatformLink('telegram', conversation);
    chatMenuContent.appendChild(telegramLink);

    if (!conversation.external_platform) {
        const externalAccessLink = createMenuLink('fa-plug', 'External access', () => openExternalAccessModal(conversation.id));
        chatMenuContent.appendChild(externalAccessLink);
    }

    // Click handler for the chat-menu-content div
    chatMenu.addEventListener('click', (e) => {
        e.stopPropagation();

        const isCurrentlyOpen = chatMenuContent.style.display === 'block';

        closeAllChatMenus();

        if (!isCurrentlyOpen) {
            const buttonRect = chatMenu.getBoundingClientRect();

            // Portal to body to escape all overflow clipping containers
            chatMenuContent._originParent = chatMenu;
            document.body.appendChild(chatMenuContent);

            chatMenuContent.style.position = 'fixed';
            chatMenuContent.style.right = (window.innerWidth - buttonRect.right) + 'px';
            chatMenuContent.style.left = 'auto';
            chatMenuContent.style.top = (buttonRect.bottom + 2) + 'px';
            chatMenuContent.style.zIndex = '9999';
            chatMenuContent.style.display = 'block';

            // Lazy-load platform mode checkmarks on first menu open
            chatMenuContent.querySelectorAll('.platform-menu-container').forEach(container => {
                if (container._platformLazyLoad && !container._platformLazyLoad.loaded) {
                    const { conversationId, platform, voiceModeLink, textModeLink } = container._platformLazyLoad;
                    loadCurrentPlatformMode(conversationId, platform, voiceModeLink, textModeLink)
                        .then(success => {
                            if (success) container._platformLazyLoad.loaded = true;
                        });
                }
            });

            const parentItem = chatMenu.closest('.list-group-item');
            if (parentItem) parentItem.style.zIndex = '10';

            // Check if menu overflows viewport bottom
            const menuRect = chatMenuContent.getBoundingClientRect();
            if (menuRect.bottom > window.innerHeight) {
                chatMenuContent.style.top = (buttonRect.top - menuRect.height - 2) + 'px';
                chatMenuContent.classList.add('menu-above');
            } else {
                chatMenuContent.classList.remove('menu-above');
            }

            // Close on sidebar scroll so the menu doesn't float detached
            const listGroup = chatMenu.closest('.list-group');
            if (listGroup) {
                listGroup.addEventListener('scroll', closeAllChatMenus, { once: true });
            }
        }
    });

    // Close menu when clicking a menu item; stop propagation to prevent parent handlers
    chatMenuContent.addEventListener('click', (e) => {
        if (e.target.closest('a')) {
            closeAllChatMenus();
        }
        e.stopPropagation();
    });

    return chatMenu;
}

const renameConversation = withSession(function(conversationId) {
    // Close menu immediately
    closeAllChatMenus();

    const conversationElement = document.querySelector(`[data-conversation-id="${conversationId}"]`);
    const nameSpan = conversationElement.querySelector('.chat-name');
    const currentName = nameSpan.textContent.trim();

    // Create an input to edit the name
    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentName;
    input.classList.add('rename-input');
    input.maxLength = 256; // Limit to 256 characters

    // Replace the span with the input
    nameSpan.replaceWith(input);
    input.focus();

    // Save icon reference before any DOM changes (WhatsApp, locked, etc.)
    const existingIcon = nameSpan.querySelector('i');

    // Clean up all rename listeners and restore span
    function exitRenameMode() {
        document.removeEventListener('click', onClickOutside);
        input.replaceWith(nameSpan);
    }

    // Function to save new name
    async function saveNewName() {
        const newName = input.value.trim().substring(0, 256); // Ensure maximum 256 characters
        if (newName && newName !== currentName) {
            try {
                const response = await secureFetch(`/api/conversations/${conversationId}/rename`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ new_name: newName }),
                });

                if (response.ok) {
                    // Preserve icon (WhatsApp, locked) when updating name
                    nameSpan.textContent = '';
                    if (existingIcon) {
                        nameSpan.appendChild(existingIcon);
                        nameSpan.appendChild(document.createTextNode(` ${newName}`));
                    } else {
                        nameSpan.textContent = newName;
                    }
                    exitRenameMode();
                    updateActiveChatName(newName);
                    return;
                } else {
                    console.error('Error renaming conversation');
                }
            } catch (error) {
                console.error('Error sending rename request:', error);
            }
        }
        exitRenameMode();
    }

    // Handle the event of pressing Enter or ESC
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            saveNewName();
        } else if (e.key === 'Escape') {
            exitRenameMode();
        }
    });

    // Handle the event of clicking outside the input
    function onClickOutside(e) {
        if (e.target !== input && !input.contains(e.target)) {
            saveNewName();
        }
    }
    document.addEventListener('click', onClickOutside);

    // Prevent the click inside the input from propagating the event
    input.addEventListener('click', (e) => {
        e.stopPropagation();
    });
});

function createMenuLink(iconClass, text, onClick, additionalClass = '') {
    const link = document.createElement('a');
    link.href = '#';
    link.classList.add('menu-link');
    if (additionalClass) link.classList.add(additionalClass);
    link.innerHTML = `<i class="fas ${iconClass}"></i> ${text}`;
    link.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        onClick();
    });
    return link;
}

function getExternalPlatformIcon(platform) {
    switch (platform.toLowerCase()) {
        case 'whatsapp':
            return 'fab fa-whatsapp';
        case 'telegram':
            return 'fab fa-telegram';
        default:
            return 'fas fa-external-link-alt';
    }
}

function getConversationExternalBindings(conversation) {
    if (!conversation || conversation.external_platform) {
        return null;
    }
    const bindings = conversation.external_bindings;
    if (!bindings || typeof bindings !== 'object') {
        return null;
    }
    return bindings;
}

function createExternalDeviceBadge(conversation) {
    const bindings = getConversationExternalBindings(conversation);
    const count = bindings ? parseInt(bindings.effective_count || 0, 10) : 0;
    if (!count || count < 1) {
        return null;
    }

    const badge = document.createElement('button');
    badge.type = 'button';
    badge.className = 'external-device-badge';
    badge.title = bindings.tooltip || `${count} external device${count === 1 ? '' : 's'}`;
    badge.setAttribute('aria-label', 'External access');
    badge.innerHTML = `<i class="fas fa-microchip"></i><span>${count}</span>`;
    badge.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        openExternalAccessModal(conversation.id);
    });
    return badge;
}

function ensureExternalAccessStyles() {
    if (document.getElementById('external-access-styles')) {
        return;
    }
    const style = document.createElement('style');
    style.id = 'external-access-styles';
    style.textContent = `
        .external-device-row {
            display: flex !important;
            align-items: center;
            gap: 0.35rem;
        }
        .external-device-row .chat-name {
            min-width: 0;
            flex: 1 1 auto;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .external-device-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 0.25rem;
            min-width: 2.15rem;
            height: 1.45rem;
            margin-left: auto;
            border: 1px solid var(--border-color, rgba(128, 128, 128, 0.35));
            border-radius: 999px;
            background: var(--input-bg, rgba(128, 128, 128, 0.12));
            color: inherit;
            font-size: 0.78rem;
            line-height: 1;
            cursor: pointer;
        }
        .external-device-badge:hover {
            background: var(--hover-bg, rgba(128, 128, 128, 0.22));
        }
        .external-device-badge i {
            font-size: 0.78rem;
        }
        .external-access-list {
            display: grid;
            gap: 0.5rem;
        }
        .external-access-option {
            display: flex;
            align-items: flex-start;
            gap: 0.65rem;
            padding: 0.65rem 0;
            border-bottom: 1px solid var(--border-color, rgba(128, 128, 128, 0.25));
        }
        .external-access-option:last-child {
            border-bottom: 0;
        }
        .external-access-icon {
            width: 1.25rem;
            text-align: center;
            opacity: 0.85;
            margin-top: 0.15rem;
        }
        .external-access-title {
            display: block;
            font-weight: 600;
        }
        .external-access-meta {
            display: block;
            font-size: 0.82rem;
            opacity: 0.78;
        }
        .external-access-section-title {
            font-size: 0.8rem;
            font-weight: 700;
            letter-spacing: 0;
            text-transform: uppercase;
            opacity: 0.72;
            margin: 0.8rem 0 0.35rem;
        }
        .external-access-empty {
            display: flex;
            flex-direction: column;
            gap: 0.65rem;
            align-items: flex-start;
            padding: 0.5rem 0;
        }
    `;
    document.head.appendChild(style);
}

function ensureExternalAccessModal() {
    ensureExternalAccessStyles();
    let modal = document.getElementById('externalAccessModal');
    if (modal) {
        return modal;
    }
    modal = document.createElement('div');
    modal.className = 'modal fade';
    modal.id = 'externalAccessModal';
    modal.tabIndex = -1;
    modal.setAttribute('aria-labelledby', 'externalAccessModalLabel');
    modal.setAttribute('aria-hidden', 'true');
    modal.innerHTML = `
        <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title" id="externalAccessModalLabel">External access</h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body" id="externalAccessModalBody"></div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                    <button type="button" class="btn btn-primary" id="externalAccessSaveBtn">Save</button>
                </div>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    modal.querySelector('#externalAccessSaveBtn').addEventListener('click', () => saveExternalAccessBindings());
    return modal;
}

function externalAccessIcon(iconClass, fallbackClass) {
    const icon = document.createElement('i');
    (iconClass || fallbackClass).split(/\s+/).filter(Boolean).forEach(className => icon.classList.add(className));
    icon.classList.add('external-access-icon');
    return icon;
}

function createExternalAccessOption(kind, item) {
    const label = document.createElement('label');
    label.className = 'external-access-option';

    const input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'form-check-input';
    input.value = item.id;
    input.dataset.kind = kind;
    input.checked = !!item.assigned;
    label.appendChild(input);

    label.appendChild(externalAccessIcon(item.icon_class, kind === 'device' ? 'fas fa-microchip' : 'fas fa-layer-group'));

    const content = document.createElement('span');
    const title = document.createElement('span');
    title.className = 'external-access-title';
    title.textContent = kind === 'device' ? item.display_name : item.name;
    content.appendChild(title);

    const metaParts = [];
    if (kind === 'device') {
        metaParts.push(item.slug);
        metaParts.push(item.device_type);
        if (!item.enabled) {
            metaParts.push('disabled');
        }
    } else {
        metaParts.push(item.slug);
        metaParts.push(`${item.member_count || 0} member${item.member_count === 1 ? '' : 's'}`);
    }
    if (item.bound_conversation_id && !item.assigned && item.bound_conversation_name) {
        metaParts.push(`currently: ${item.bound_conversation_name}`);
    }

    const meta = document.createElement('span');
    meta.className = 'external-access-meta';
    meta.textContent = metaParts.filter(Boolean).join(' · ');
    content.appendChild(meta);
    label.appendChild(content);
    return label;
}

function appendExternalAccessSection(body, titleText, items, kind) {
    if (!items || items.length === 0) {
        return;
    }
    const title = document.createElement('div');
    title.className = 'external-access-section-title';
    title.textContent = titleText;
    body.appendChild(title);

    const list = document.createElement('div');
    list.className = 'external-access-list';
    items.forEach(item => list.appendChild(createExternalAccessOption(kind, item)));
    body.appendChild(list);
}

function renderExternalAccessModal(data) {
    const modal = ensureExternalAccessModal();
    modal.dataset.conversationId = data.conversation_id;
    const body = modal.querySelector('#externalAccessModalBody');
    const saveButton = modal.querySelector('#externalAccessSaveBtn');
    body.innerHTML = '';

    if (data.external_platform) {
        const message = document.createElement('p');
        message.textContent = 'External devices are not available on WhatsApp or Telegram conversations.';
        body.appendChild(message);
        saveButton.style.display = 'none';
        return modal;
    }

    const devices = Array.isArray(data.devices) ? data.devices : [];
    const groups = Array.isArray(data.groups) ? data.groups : [];
    if (devices.length === 0 && groups.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'external-access-empty';
        const text = document.createElement('span');
        text.textContent = 'No devices yet.';
        const link = document.createElement('a');
        link.href = '/admin/devices';
        link.textContent = 'Open Devices';
        empty.appendChild(text);
        empty.appendChild(link);
        body.appendChild(empty);
        saveButton.style.display = 'none';
        return modal;
    }

    const summary = data.external_bindings || {};
    const summaryLine = document.createElement('p');
    summaryLine.className = 'external-access-meta';
    summaryLine.textContent = `${summary.effective_count || 0} effective device${summary.effective_count === 1 ? '' : 's'}`;
    body.appendChild(summaryLine);

    appendExternalAccessSection(body, 'Devices', devices, 'device');
    appendExternalAccessSection(body, 'Groups', groups, 'group');
    saveButton.style.display = '';
    return modal;
}

const openExternalAccessModal = withSession(async function(conversationId) {
    try {
        const response = await secureFetch(`/api/conversations/${conversationId}/external-bindings`);
        if (!response) return;
        const data = await response.json();
        if (!response.ok || !data.success) {
            NotificationModal.error('External Access', data.message || 'Could not load external access.');
            return;
        }
        const modal = renderExternalAccessModal(data);
        bootstrap.Modal.getOrCreateInstance(modal).show();
    } catch (error) {
        console.error('External access load failed:', error);
        NotificationModal.error('External Access', 'Could not load external access.');
    }
});

const saveExternalAccessBindings = withSession(async function() {
    const modal = ensureExternalAccessModal();
    const conversationId = modal.dataset.conversationId;
    const body = modal.querySelector('#externalAccessModalBody');
    const deviceIds = Array.from(body.querySelectorAll('input[data-kind="device"]:checked')).map(input => parseInt(input.value, 10));
    const groupIds = Array.from(body.querySelectorAll('input[data-kind="group"]:checked')).map(input => parseInt(input.value, 10));

    try {
        const response = await secureFetch(`/api/conversations/${conversationId}/external-bindings`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                device_ids: deviceIds,
                group_ids: groupIds,
            }),
        });
        if (!response) return;
        const data = await response.json();
        if (!response.ok || !data.success) {
            NotificationModal.error('External Access', data.message || 'Could not save external access.');
            return;
        }
        updateConversationExternalBindings(conversationId, data);
        bootstrap.Modal.getOrCreateInstance(modal).hide();
        NotificationModal.toast('External access updated', 'success');
    } catch (error) {
        console.error('External access save failed:', error);
        NotificationModal.error('External Access', 'Could not save external access.');
    }
});

function updateConversationExternalBindings(conversationId, data) {
    const externalChatsContainer = document.querySelector('#external-chats-container');
    const dynamicChatsContainer = document.querySelector('#dynamic-chats-container');
    const affected = data.affected_conversations && typeof data.affected_conversations === 'object'
        ? data.affected_conversations
        : {};
    const affectedIds = new Set(Object.keys(affected));
    affectedIds.add(String(conversationId));

    affectedIds.forEach((affectedId) => {
        const updatedBindings = String(affectedId) === String(conversationId)
            ? data.external_bindings || affected[affectedId] || null
            : affected[affectedId] || null;
        document.querySelectorAll(
            `#dynamic-chats-container [data-conversation-id="${affectedId}"], ` +
            `#external-chats-container [data-conversation-id="${affectedId}"]`
        ).forEach(element => {
            const currentData = element._conversationData || {
                id: parseInt(affectedId, 10),
                chat_name: element.querySelector('.chat-name')?.textContent?.trim() || `Chat ${affectedId}`,
            };
            currentData.external_bindings = updatedBindings;
            currentData.external_platform = String(affectedId) === String(conversationId)
                ? data.external_platform || currentData.external_platform || element.dataset.externalPlatform || null
                : currentData.external_platform || element.dataset.externalPlatform || null;
            updateSingleConversation(element, currentData, externalChatsContainer, dynamicChatsContainer);
        });
        updateFolderConversationExternalBindings(affectedId, updatedBindings);
    });
    sortDynamicChats();
}

function updateFolderConversationExternalBindings(conversationId, externalBindings) {
    document.querySelectorAll(`.folder-chat-item[data-conversation-id="${conversationId}"]`).forEach(element => {
        const currentData = element._conversationData || {
            id: parseInt(conversationId, 10),
            chat_name: element.querySelector('.chat-name')?.textContent?.trim() || `Chat ${conversationId}`,
        };
        currentData.external_bindings = externalBindings;
        element._conversationData = currentData;
        element.querySelectorAll('.external-device-badge').forEach(badge => badge.remove());
        const badge = createExternalDeviceBadge(currentData);
        if (badge) {
            element.classList.add('external-device-row');
            const chatContent = element.querySelector('.chat-content-container');
            const chatMenu = element.querySelector('.chat-menu');
            if (chatContent && chatMenu) {
                chatContent.insertBefore(badge, chatMenu);
            }
        } else {
            element.classList.remove('external-device-row');
        }
    });
}

function createPlatformLink(platform, conversation) {
    const isAssigned = conversation.external_platform === platform;
    const icon = platform === 'whatsapp' ? 'fa-whatsapp' : 'fa-telegram';
    const platformName = platform.charAt(0).toUpperCase() + platform.slice(1);

    if (isAssigned) {
        // Create platform submenu container (works for both WhatsApp and Telegram)
        const container = document.createElement('div');
        container.className = 'platform-menu-container';

        // Main platform option (remove)
        const mainLink = document.createElement('a');
        mainLink.href = '#';
        mainLink.classList.add('platform-link');
        mainLink.innerHTML = `<i class="fab ${icon}"></i> Remove from ${platformName}`;
        mainLink.addEventListener('click', function(e) {
            e.stopPropagation();
            closeAllChatMenus();
            toggleExternalPlatform(conversation.id, platform, isAssigned);
        });

        // Mode separator
        const modeSeparator = document.createElement('div');
        modeSeparator.classList.add('menu-separator');

        // Voice mode option
        const voiceModeLink = document.createElement('a');
        voiceModeLink.href = '#';
        voiceModeLink.classList.add('platform-link', 'platform-mode-option');
        voiceModeLink.innerHTML = `<i class="fas fa-microphone"></i> <span class="mode-text">Voice Mode</span> <span class="mode-check" style="display: none;">✓</span>`;
        voiceModeLink.addEventListener('click', function(e) {
            e.stopPropagation();
            closeAllChatMenus();
            changePlatformMode(conversation.id, platform, 'voice');
        });

        // Text mode option
        const textModeLink = document.createElement('a');
        textModeLink.href = '#';
        textModeLink.classList.add('platform-link', 'platform-mode-option');
        textModeLink.innerHTML = `<i class="fas fa-keyboard"></i> <span class="mode-text">Text Mode</span> <span class="mode-check" style="display: none;">✓</span>`;
        textModeLink.addEventListener('click', function(e) {
            e.stopPropagation();
            closeAllChatMenus();
            changePlatformMode(conversation.id, platform, 'text');
        });

        container.appendChild(mainLink);
        container.appendChild(modeSeparator);
        container.appendChild(voiceModeLink);
        container.appendChild(textModeLink);

        // Store references for lazy loading when menu opens
        container._platformLazyLoad = {
            conversationId: conversation.id,
            platform: platform,
            voiceModeLink: voiceModeLink,
            textModeLink: textModeLink,
            loaded: false
        };

        return container;
    } else {
        const otherPlatform = conversation.external_platform;

        const link = document.createElement('a');
        link.href = '#';
        link.classList.add('platform-link');
        link.innerHTML = `<i class="fab ${icon}"></i> Use for ${platformName}`;
        link.addEventListener('click', function(e) {
            e.stopPropagation();
            closeAllChatMenus();
            if (otherPlatform && otherPlatform !== platform) {
                const otherName = otherPlatform === 'whatsapp' ? 'WhatsApp' : 'Telegram';
                const targetName = platform === 'whatsapp' ? 'WhatsApp' : 'Telegram';
                NotificationModal.confirm(
                    'Move Conversation',
                    `This conversation is on ${otherName}. Move it to ${targetName}?`,
                    () => toggleExternalPlatform(conversation.id, platform, isAssigned)
                );
                return;
            }
            toggleExternalPlatform(conversation.id, platform, isAssigned);
        });
        return link;
    }
}

const toggleExternalPlatform = withSession(function(conversationId, platform, isAssigned) {
    const action = isAssigned ? 'remove' : 'add';
    const visibleCount = getVisibleConversationsCount();
    secureFetch(`/api/conversations/${conversationId}/external-platform`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ platform, action, visible_count: visibleCount })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            const updatedConversation = data.updatedConversations.find(
                conv => conv.id === parseInt(conversationId)
            );

            if (updatedConversation) {
                updateConversationElement(conversationId, updatedConversation, data.updatedConversations);
            } else {
                const externalChatsContainer = document.querySelector('#external-chats-container');
                const dynamicChatsContainer = document.querySelector('#dynamic-chats-container');

                document.querySelectorAll(`[data-conversation-id="${conversationId}"]`).forEach(el => el.remove());

                data.updatedConversations.forEach(conv => {
                    const existingElement = document.querySelector(`[data-conversation-id="${conv.id}"]`);
                    if (existingElement) {
                        updateSingleConversation(existingElement, conv, externalChatsContainer, dynamicChatsContainer);
                    } else {
                        const newElement = document.createElement('a');
                        updateSingleConversation(newElement, conv, externalChatsContainer, dynamicChatsContainer);
                    }
                });

                sortDynamicChats();
                updateExternalSection();
            }
        } else if (data.error === 'no_phone_number') {
            showPhoneRequiredModal(platform);
        } else if (data.message) {
            NotificationModal.error('Assignment Error', data.message);
        } else {
            console.error('Error updating external platform:', data.error);
        }
    })
    .catch(error => {
        console.error('Error:', error);
        NotificationModal.error('Assignment Error', 'Could not update the external platform assignment.');
    });
});

function updateConversationElement(conversationId, updatedConversation, allConversations) {
    const externalChatsContainer = document.querySelector('#external-chats-container');
    const dynamicChatsContainer = document.querySelector('#dynamic-chats-container');

    // First, remove all existing instances of updated conversation
    document.querySelectorAll(`[data-conversation-id="${conversationId}"]`).forEach(el => el.remove());

    // Then, update or create conversation element
    const element = document.createElement('a');
    updateSingleConversation(element, updatedConversation, externalChatsContainer, dynamicChatsContainer);

    // Update other conversations if necessary
    allConversations.forEach(conv => {
        if (conv.id !== conversationId) {
            const existingElement = document.querySelector(`[data-conversation-id="${conv.id}"]`);
            if (existingElement) {
                updateSingleConversation(existingElement, conv, externalChatsContainer, dynamicChatsContainer);
            } else {
                const newElement = document.createElement('a');
                updateSingleConversation(newElement, conv, externalChatsContainer, dynamicChatsContainer);
            }
        }
    });

    if (updatedConversation.external_platform) {
        // Move to external section (without clearing other platform conversations)
        externalChatsContainer.appendChild(element);
        document.querySelector('.external-section').style.display = 'block';
    } else {
        // Move to dynamic container if not external
        dynamicChatsContainer.appendChild(element);
    }
    // Sort conversations in dynamic container
    sortDynamicChats();

    // Hide external section if empty
    if (externalChatsContainer.children.length === 0) {
        document.querySelector('.external-section').style.display = 'none';
    } else {
        document.querySelector('.external-section').style.display = 'block';
    }
}

function getVisibleConversationsCount() {
    const externalCount = document.querySelector('#external-chats-container').children.length;
    const dynamicCount = document.querySelector('#dynamic-chats-container').children.length;
    return externalCount + dynamicCount;
}

function sortDynamicChats() {
    const container = document.getElementById('dynamic-chats-container');
    const elements = Array.from(container.children);
    elements.sort((a, b) => {
        const actA = a.dataset.lastActivity || '';
        const actB = b.dataset.lastActivity || '';
        if (actA !== actB) return actB.localeCompare(actA);
        return parseInt(b.dataset.conversationId) - parseInt(a.dataset.conversationId);
    });
    elements.forEach(el => container.appendChild(el));
}

function updateSingleConversation(element, conversationData, externalContainer, dynamicContainer) {
    if (conversationData.hidden_from_history || conversationData.is_incognito) {
        if (element instanceof HTMLElement) {
            element.remove();
        }
        return;
    }
    // Ensure we always have a valid name
    const chatName = conversationData.chat_name || `Chat ${conversationData.id}`;
    let targetContainer;

    if (conversationData.external_platform) {
        targetContainer = externalContainer;
    } else {
        targetContainer = dynamicContainer;
    }

    // Update existing element or create a new one
    if (!(element instanceof HTMLElement)) {
        element = document.createElement('a');
    }
    const wasActive = element.classList.contains('active-chat');
    element.href = '#';
    element.className = 'list-group-item list-group-item-action';
    if (wasActive) {
        element.classList.add('active-chat');
    }
    element.dataset.conversationId = conversationData.id;
    element.dataset.lastActivity = conversationData.last_activity || '';
    element.dataset.machine = conversationData.machine || 'undefined';
    element.dataset.llmModel = conversationData.llm_model || '';
    if (conversationData.llm_id) {
        element.dataset.llmId = String(conversationData.llm_id);
    } else {
        delete element.dataset.llmId;
    }
    if (conversationData.prompt_id) {
        element.dataset.promptId = String(conversationData.prompt_id);
    } else {
        delete element.dataset.promptId;
    }
    element.dataset.locked = conversationData.locked ? 'true' : 'false';
    element.dataset.webSearchAllowed = conversationData.web_search_allowed !== false ? 'true' : 'false';
    element.dataset.webSearchForced = conversationData.web_search_forced === true ? 'true' : 'false';
    element.dataset.isIncognito = conversationData.is_incognito ? 'true' : 'false';
    element.dataset.isPaid = conversationData.is_paid ? '1' : '0';
    if (conversationData.locked) {
        element.classList.add('conversation-locked');
    }
    if (conversationData.forced_llm_id) {
        element.dataset.forcedLlmId = conversationData.forced_llm_id;
    } else {
        delete element.dataset.forcedLlmId;
    }
    if (conversationData.hide_llm_name) {
        element.dataset.hideLlmName = 'true';
    } else {
        delete element.dataset.hideLlmName;
    }
    if (conversationData.allowed_llms) {
        element.dataset.allowedLlms = JSON.stringify(conversationData.allowed_llms);
    } else {
        delete element.dataset.allowedLlms;
    }
    element._conversationData = conversationData;
    if (conversationData.external_platform) {
        element.dataset.externalPlatform = conversationData.external_platform;
    } else {
        delete element.dataset.externalPlatform;
    }

    // Wrap name in .chat-name span (matches addConversationElement structure)
    const nameSpan = document.createElement('span');
    nameSpan.className = 'chat-name';
    if (conversationData.external_platform) {
        const iconClass = getExternalPlatformIcon(conversationData.external_platform);
        nameSpan.innerHTML = `<i class="${iconClass}"></i> ${chatName}`;
    } else if (conversationData.locked) {
        nameSpan.innerHTML = `<i class="fas fa-comment-slash" title="This conversation is locked"></i> ${chatName}`;
    } else {
        nameSpan.textContent = chatName;
    }
    delete element.dataset.folderMenuEnhanced;
    element.innerHTML = '';
    element.appendChild(nameSpan);

    const externalDeviceBadge = createExternalDeviceBadge(conversationData);
    if (externalDeviceBadge) {
        element.classList.add('external-device-row');
        element.appendChild(externalDeviceBadge);
    } else {
        element.classList.remove('external-device-row');
    }

    const chatMenu = createChatMenu(conversationData);
    element.appendChild(chatMenu);

    if (element.parentElement !== targetContainer) {
        if (targetContainer === externalContainer) {
            const existingForPlatform = externalContainer.querySelector(
                `[data-external-platform="${conversationData.external_platform}"]`
            );
            if (existingForPlatform && existingForPlatform !== element) {
                existingForPlatform.remove();
            }
            externalContainer.appendChild(element);
        } else {
            dynamicContainer.appendChild(element); 
        }
    } else if (!element.parentElement) {
        targetContainer.appendChild(element);
    }

    setupConversationElementListeners(element);
}

function setupConversationElementListeners(element) {
    element.removeEventListener('click', conversationClickHandler);
    element.addEventListener('click', conversationClickHandler);
}

function conversationClickHandler(e) {
    if (!e.target.closest('.chat-menu') && !e.target.closest('.external-device-badge')) {
        var conversationId = this.getAttribute('data-conversation-id');
        var chatNameElement = this.querySelector('.chat-name');
        var chatName = chatNameElement ? chatNameElement.textContent.trim() : `Chat ${conversationId}`;
        var machine = this.getAttribute('data-machine');
        
        if (conversationId) {
            if (typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN) {
                stopAudioAndCloseWebSocket();
            }
            
            // Remove active-chat class from ALL chats everywhere
            document.querySelectorAll('.active-chat').forEach(el => {
                el.classList.remove('active-chat');
            });
            
            // Add active-chat class to this element
            this.classList.add('active-chat');
            
            // Update global selectedChat variable
            window.selectedChat = this;
            document.getElementById('my-bookmarks-btn')?.classList.remove('active-bookmarks');
            
            continueConversation(conversationId, chatName, machine);
        }
    }
}

function getPlatformFromElement(element) {
    const whatsappIcon = element.querySelector('.fa-whatsapp');
    const telegramIcon = element.querySelector('.fa-telegram');
    if (whatsappIcon) return 'whatsapp';
    if (telegramIcon) return 'telegram';
    return null;
}

function deactivateChat() {
    // Hide the input box and buttons
    document.getElementById('message-input-container').style.display = 'none';
    
    // Add translucent layer over chat-window
    const chatWindow = document.getElementById('window-chat');
    let overlay = document.getElementById('chat-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'chat-overlay';
        chatWindow.appendChild(overlay);
    }
    overlay.style.display = 'block';

    // Update global state
    beginConversationViewTransition();
    currentConversationId = null;
    setCurrentProviderHealth(null);
}

function removeOverlay() {
    const overlay = document.getElementById('chat-overlay');
    if (overlay) {
        overlay.style.display = 'none';
    }
    document.getElementById('message-input-container').style.display = 'flex';
}


function removeConversationElement(conversationId) {
    const conversationElement = document.querySelector(`[data-conversation-id="${conversationId}"]`);
    if (conversationElement) {
        conversationElement.remove();
    }
}

function moveConversationToTop(conversationId) {
    const element = document.querySelector(`[data-conversation-id="${conversationId}"]`);
    if (!element) return;
    const container = element.parentElement;
    if (container && container.firstChild !== element) {
        container.insertBefore(element, container.firstChild);
        // Update the data attribute to current time
        element.dataset.lastActivity = new Date().toISOString();
    }
}

function loadConversations(loadMore = false, isInit = false) {
    if (allConversationsLoaded && !isInit) return Promise.resolve();
    if (isLoadingConversations && !isInit) return Promise.resolve();
    isLoadingConversations = true;

    // Use embedded data on initial load (avoids HTTP request)
    if (isInit && !loadMore && typeof embeddedInitialConversations !== 'undefined' && embeddedInitialConversations !== null) {
        const conversations = embeddedInitialConversations;
        embeddedInitialConversations = null; // Clear to avoid reuse
        return processConversations(conversations, loadMore, isInit)
            .finally(() => { isLoadingConversations = false; });
    }

    var url = `/api/conversations?user_id=${user_id}&limit=${limit}`;
    if (oldestLoadedActivity !== null && oldestLoadedId !== null) {
        url += `&before_activity=${encodeURIComponent(oldestLoadedActivity)}&before_id=${oldestLoadedId}`;
    }

    // By default, only load conversations not in folders (loose conversations)
    // folder_id parameter when null/undefined will return conversations not in folders
    // This ensures the main chat list only shows loose conversations

    return fetch(url)
        .then(response => response.json())
        .then(conversations => processConversations(conversations, loadMore, isInit))
        .catch(error => {
            console.error('Error loading conversations:', error);
            enableInputControls();
            document.getElementById('loading-indicator').style.display = 'none';
        })
        .finally(() => { isLoadingConversations = false; });
}

function processConversations(conversations, loadMore, isInit) {
    if (conversations.length === 0) {
        allConversationsLoaded = true;
        document.getElementById('load-more-button').style.display = 'none';
        if (isInit) {
            return startNewConversation();
        }
        return Promise.resolve();
    }

    conversations.forEach(conversation => {
        addConversationElement(conversation, conversation.chat_name, currentConversationId);
    });

    // Update pagination cursor from the last non-external conversation in this batch
    // (the oldest by activity, since the API returns newest-first)
    for (let i = conversations.length - 1; i >= 0; i--) {
        const conv = conversations[i];
        if (!conv.external_platform) {
            oldestLoadedActivity = conv.last_activity || null;
            oldestLoadedId = conv.id;
            break;
        }
    }

    if (conversations.length < limit) {
        allConversationsLoaded = true;
        document.getElementById('load-more-button').style.display = 'none';
    } else {
        document.getElementById('load-more-button').style.display = 'block';
    }

    if (loadMore && conversations.length > 0) {
        const firstNewElement = document.querySelector(`[data-conversation-id="${conversations[0].id}"]`);
        if (firstNewElement) {
            firstNewElement.scrollIntoView({ behavior: 'smooth' });
        }
    }

    if (isInit && currentConversationId !== null) {
        const existingConversation = conversations.find(conv => conv.id === currentConversationId);
        if (existingConversation) {
            return continueConversation(currentConversationId, existingConversation.chat_name, existingConversation.machine, isInit, null, existingConversation);
        } else {
            // Load the most recent conversation instead of creating a new one
            if (conversations && conversations.length > 0) {
                const mostRecent = conversations[0]; // Conversations are ordered by date
                return continueConversation(mostRecent.id, mostRecent.chat_name, mostRecent.machine, isInit, null, mostRecent);
            }
            // Only create new chat if there are no conversations at all
            return startNewConversation();
        }
    }

    return Promise.resolve();
}

function loadMoreConversations() {
    loadConversations(true);
}

function continueConversation(
    conversationId,
    chatName,
    machine,
    isInit = false,
    targetMessageId = null,
    conversationData = null,
    requestedGeneration = null
) {
    // Check if this conversation is already loaded (compare with current conversation ID)
    if (currentConversationId &&
        currentConversationId.toString() === conversationId.toString() &&
        !isInit && !targetMessageId) {
        return Promise.resolve();
    }

    const viewGeneration = requestedGeneration === null
        ? beginConversationViewTransition()
        : requestedGeneration;
    if (viewGeneration !== conversationViewGeneration) {
        return Promise.resolve();
    }

    if (currentConversationIncognito &&
        currentConversationId &&
        currentConversationId.toString() !== conversationId.toString()) {
        return maybeCloseCurrentIncognitoBeforeLeaving().then(canContinue => {
            if (!canContinue) {
                if (viewGeneration === conversationViewGeneration) {
                    enableInputControls();
                }
                return Promise.resolve();
            }
            if (viewGeneration !== conversationViewGeneration) {
                return Promise.resolve();
            }
            return continueConversation(
                conversationId,
                chatName,
                machine,
                isInit,
                targetMessageId,
                conversationData,
                viewGeneration
            );
        });
    }

    removeOverlay();
    if (window.ChatWarmup && typeof window.ChatWarmup.resetForConversation === 'function') {
        window.ChatWarmup.resetForConversation(conversationId);
    }

    // Same conversation but jumping to a specific message: reset state and reload
    if (currentConversationId &&
        currentConversationId.toString() === conversationId.toString() &&
        targetMessageId) {
        oldestLoadedMessageId = null;
        allMessagesLoaded = false;
        processedMessageIds.clear();
        document.getElementById('chat-messages-container').innerHTML = '';
        return loadMessages(conversationId, false, targetMessageId, 0, viewGeneration);
    }

    const selectedChat = conversationIdsMatch(
        window.selectedChat?.dataset?.conversationId,
        conversationId
    )
        ? window.selectedChat
        : document.querySelector(`[data-conversation-id="${conversationId}"]`);

    hideScrollNavButtons();
    oldestLoadedMessageId = null;
    allMessagesLoaded = false;
    processedMessageIds.clear();
    currentConversationId = conversationId;
    const isIncognitoConversation = isConversationIncognitoData(conversationData, selectedChat);
    if (isIncognitoConversation) {
        localStorage.removeItem('activeConversationId');
    } else {
        localStorage.setItem('activeConversationId', conversationId);
    }
    setIncognitoUiState(isIncognitoConversation);
    const dateOptions = { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' };
    const formattedStartDate = new Date(startDate).toLocaleDateString(undefined, dateOptions);

    // Extract only chat name, ignoring menu
    let chatTitleText = "New Chat";
    if (selectedChat && selectedChat.firstChild) {
        chatTitleText = selectedChat.firstChild.textContent.trim();
    } else if (chatName) {
        chatTitleText = chatName;
    }
    document.querySelector('.chatbot-info h4').textContent = `${chatTitleText}`;
    
    // Deactivate all chats and My Bookmarks
    document.querySelectorAll('.list-group-item-action').forEach(function(item) {
        item.classList.remove('active-chat', 'previously-active-chat', 'active-bookmarks');
    });

    // Activate the selected chat
    if (selectedChat) {
        selectedChat.classList.add('active-chat');
    }
    // Disable input controls before loading messages
    disableInputControls();

    return new Promise((resolve) => {
        loadMessages(conversationId, false, targetMessageId, 0, viewGeneration).then(loadResult => {
            if (!loadResult ||
                !isCurrentConversationView(conversationId, viewGeneration)) {
                resolve();
                return;
            }

            setupInfiniteScroll(conversationId, viewGeneration);

            // Show and correctly configure the input box and other chat-related elements
            const messageInputContainer = document.getElementById('message-input-container');
            messageInputContainer.style.display = 'flex';
            messageInputContainer.style.justifyContent = 'center';

            const formMessage = document.getElementById('form-message');
            formMessage.style.display = 'flex';
            formMessage.style.justifyContent = 'center';
            formMessage.style.width = '100%';
            formMessage.style.maxWidth = '48rem';

            const messageText = document.getElementById('message-text');
            messageText.focus();

            if (typeof window.closeSidebar === 'function') {
                window.closeSidebar();
            }

            // Get llm_model from conversation element or passed data
            const llmModel = selectedChat?.dataset?.llmModel || conversationData?.llm_model || null;
            const rawLlmId = selectedChat?.dataset?.llmId || conversationData?.llm_id || null;
            const llmId = rawLlmId ? parseInt(rawLlmId, 10) : null;
            updateChatHeader(conversationId, chatTitleText, llmModel, llmId, viewGeneration);

            // Apply model restrictions from conversation data
            const forcedLlmId = selectedChat?.dataset?.forcedLlmId || conversationData?.forced_llm_id || null;
            const hideLlmName = selectedChat?.dataset?.hideLlmName === 'true' || conversationData?.hide_llm_name === true;
            let allowedLlms = null;
            if (selectedChat?.dataset?.allowedLlms) {
                try { allowedLlms = JSON.parse(selectedChat.dataset.allowedLlms); } catch(e) {}
            } else if (conversationData?.allowed_llms) {
                allowedLlms = conversationData.allowed_llms;
            }
            if (window.modelSelector) {
                window.modelSelector.applyRestrictions(
                    forcedLlmId ? parseInt(forcedLlmId) : null,
                    hideLlmName,
                    allowedLlms
                );
            }

            // Update Multi-AI state on conversation change
            if (window.multiAiManager) {
                window.multiAiManager.onConversationChange();
                window.multiAiManager.updateVisibility();
            }

            // Initialize extension selector from conversation data
            if (conversationData && conversationData.extensions_enabled && window.extensionSelector) {
                window.extensionSelector.init(
                    conversationData.extensions,
                    conversationData.active_extension,
                    conversationData.extensions_free_selection
                );
            } else if (window.extensionSelector) {
                window.extensionSelector.hide();
            }

            // Check if conversation is locked (from DOM element or passed data)
            isCurrentConversationLocked = selectedChat?.dataset?.locked === 'true' || conversationData?.locked === true;
            const lockedBanner = document.getElementById('locked-conversation-banner');

            if (isCurrentConversationLocked) {
                // Show locked banner and disable input (but not loading indicator)
                if (lockedBanner) lockedBanner.style.display = 'flex';
                messageText.placeholder = 'This conversation is locked';
                messageText.disabled = true;
                document.querySelector('#form-message button[type="submit"]').disabled = true;
                document.getElementById('loading-indicator').style.display = 'none';
            } else {
                // Hide locked banner and enable input
                if (lockedBanner) lockedBanner.style.display = 'none';
                enableInputControls();
                messageText.placeholder = 'Type a message...';
                messageText.disabled = false;
            }

            // Balance-based input visibility (runs after locked check). A linked
            // GPTSub model is an owned credential, like BYOK, but paid prompt
            // creator markup still requires the minimum Aurvek balance.
            if (!isCurrentConversationLocked &&
                typeof window.updateConversationBalanceAvailability === 'function') {
                const isPaid = conversationData != null
                    ? !!conversationData.is_paid
                    : (selectedChat?.dataset?.isPaid === '1');
                window.updateConversationBalanceAvailability(isPaid);
            }

            showPromptInfo();

            // Initialize web search toggle control with data from conversation element or passed data
            let webSearchAllowedByPrompt = null;
            if (selectedChat?.dataset?.webSearchAllowed !== undefined) {
                webSearchAllowedByPrompt = selectedChat.dataset.webSearchAllowed === 'true';
            } else if (conversationData?.web_search_allowed !== undefined) {
                webSearchAllowedByPrompt = conversationData.web_search_allowed;
            }
            let webSearchForcedByPrompt = false;
            if (selectedChat?.dataset?.webSearchForced !== undefined) {
                webSearchForcedByPrompt = selectedChat.dataset.webSearchForced === 'true';
            } else if (conversationData?.web_search_forced !== undefined) {
                webSearchForcedByPrompt = conversationData.web_search_forced;
            }
            initWebSearchControl(conversationId, webSearchAllowedByPrompt, webSearchForcedByPrompt);

            resolve();
        });
    });
}

function isMyBookmarksView() {
    return document.querySelector('.chatbot-info h4').textContent === "My Bookmarks";
}

function updateChatHeader(
    conversationId,
    chatName,
    llmModel = null,
    llmId = null,
    viewGeneration = conversationViewGeneration
) {
    if (!isCurrentConversationView(conversationId, viewGeneration)) {
        return;
    }
    const chatTitle = document.getElementById('chat-title');
    const chatModel = document.getElementById('chat-model');
    const chatTitleAvatar = document.getElementById('chat-title-avatar');
    const dateOptions = { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' };
    const formattedStartDate = new Date(startDate).toLocaleDateString(undefined, dateOptions);

    chatTitleAvatar.innerHTML = ''; // Clear the container
    const botAvatar = createAvatar('bot');
    chatTitleAvatar.appendChild(botAvatar);

    chatTitleAvatar.style.display = 'block';

    // Restore model selector visibility (hidden in bookmarks view)
    const modelSelectorContainer = document.querySelector('.model-selector-container');
    if (modelSelectorContainer) modelSelectorContainer.style.display = '';

    // Set clean title (without date or prompt)
    chatTitle.textContent = chatName;
    chatTitle.title = `Created: ${formattedStartDate}`; // Show date on hover

    // Use provided model or fetch from API as fallback
    const parsedHeaderLlmId = parseInt(llmId, 10);
    if (llmModel && Number.isInteger(parsedHeaderLlmId) && parsedHeaderLlmId > 0) {
        const modelData = (window.availableModels || []).find(
            model => Number(model.id) === Number(llmId)
        );
        const displayName = modelData?.display_name || llmModel;
        chatModel.textContent = (window.modelSelector && window.modelSelector.hideLlmName) ? 'AI' : displayName;
        if (window.modelSelector) {
            window.modelSelector.updateCurrentModel(llmModel, llmId);
        }
    } else {
        // Fallback: fetch from API (for new conversations without cached data)
        window.conversationModelIdentityUnknown = true;
        const detailsIdentityRevision = window.modelSelector?.identityRevision || 0;
        const detailsConversationRevision = window.modelSelector
            ?.getConversationIdentityRevision(conversationId) || 0;
        if (conversationDetailsController) {
            conversationDetailsController.abort();
        }
        const detailsController = new AbortController();
        conversationDetailsController = detailsController;
        fetch(`/api/conversations/${conversationId}/details`, {
            signal: detailsController.signal
        })
            .then(response => {
                if (!isCurrentConversationView(conversationId, viewGeneration)) {
                    return null;
                }
                return response.json();
            })
            .then(data => {
                if (!data || !isCurrentConversationView(conversationId, viewGeneration)) {
                    return;
                }
                const modelInfo = data.model || 'Unknown Model';
                const modelData = (window.availableModels || []).find(
                    model => Number(model.id) === Number(data.llm_id)
                );
                const modelIdentityStillCurrent = !window.modelSelector ||
                    ((window.modelSelector.identityRevision || 0) ===
                        detailsIdentityRevision &&
                    window.modelSelector.getConversationIdentityRevision(
                        conversationId
                    ) === detailsConversationRevision);

                // A model PATCH may have committed while this GET was in
                // flight. Apply restrictions from the response below, but
                // never let its older identity overwrite the committed model.
                if (modelIdentityStillCurrent) {
                    chatModel.textContent = modelData?.display_name || modelInfo;
                }
                if (window.modelSelector && modelIdentityStillCurrent) {
                    window.modelSelector.updateCurrentModel(modelInfo, data.llm_id);
                }

                // Apply restrictions from conversation details
                if (window.modelSelector) {
                    window.modelSelector.applyRestrictions(
                        data.forced_llm_id || null,
                        data.hide_llm_name || false,
                        data.allowed_llms || null
                    );
                    if (window.modelSelector.hideLlmName) {
                        chatModel.textContent = 'AI';
                    }
                }

                // Update Multi-AI state on conversation change (API fallback path)
                if (window.multiAiManager) {
                    window.multiAiManager.onConversationChange();
                    window.multiAiManager.updateVisibility();
                }
            })
            .catch(error => {
                if (error.name === 'AbortError' ||
                    !isCurrentConversationView(conversationId, viewGeneration)) {
                    return;
                }
                console.error('Error fetching conversation details:', error);
                chatModel.textContent = '';
            })
            .finally(() => {
                if (conversationDetailsController === detailsController) {
                    conversationDetailsController = null;
                }
            });
    }
}



function convertToLocalTime(utcTimestamp) {
    
    const date = new Date(utcTimestamp + 'Z');  // Add 'Z' to force UTC

    const localTimeString = date.toLocaleString('en-CA', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
        timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone
    });
    
    const formattedTime = localTimeString.replace(/(\d+)\/(\d+)\/(\d+)/, '$3/$2/$1');
    
    return {
        originalUtc: utcTimestamp,
        localTime: formattedTime
    };
}

function processMessage(
    message,
    container,
    prepend = false,
    processedIds = processedMessageIds
) {
    if (processedIds.has(message.id)) return;
    const timestamps = convertToLocalTime(message.date);
    let messageObj;

    try {
        const parsedMessage = JSON.parse(message.message);
        if (
            parsedMessage &&
            typeof parsedMessage === 'object' &&
            !Array.isArray(parsedMessage) &&
            parsedMessage.multi_ai === true &&
            Array.isArray(parsedMessage.responses)
        ) {
            messageObj = {
                type: 'multi_ai',
                responses: parsedMessage.responses,
                is_bookmarked: message.is_bookmarked,
                conversation_id: message.conversation_id
            };

            addMessage(
                message.type,
                null,
                { timestamp: timestamps, isNewMessage: false },
                false,
                messageObj,
                prepend,
                container,
                message.id,
                message.citations
            );
            processedIds.add(message.id);
            return;
        }

        if (Array.isArray(parsedMessage)) {
            parsedMessage.forEach(item => {
                if (item.type === 'text') {
                    messageObj = {
                        type: 'text',
                        text: String(item.text),
                        is_bookmarked: message.is_bookmarked,
                        conversation_id: message.conversation_id
                    };
                } else if (item.type === 'image_url') {
                    messageObj = {
                        type: 'image_url',
                        url: item.image_url.url,
                        fullsize_url: item.image_url.fullsize_url,
                        attachment_ref: item.image_url.attachment_ref,
                        alt: item.image_url.alt,
                        is_bookmarked: message.is_bookmarked,
                        conversation_id: message.conversation_id
                    };
                } else if (item.type === 'video_url') {
                    messageObj = {
                        type: 'video_url',
                        url: item.video_url.url,
                        alt: item.video_url.alt,
                        mime_type: item.video_url.mime_type,
                        poster: item.video_url.poster,
                        is_bookmarked: message.is_bookmarked,
                        conversation_id: message.conversation_id
                    };
                } else if (item.type === 'document_url') {
                    messageObj = {
                        type: 'document_url',
                        url: item.document_url.url,
                        filename: item.document_url.filename || 'document.pdf',
                        pages: item.document_url.pages || 0,
                        is_bookmarked: message.is_bookmarked,
                        conversation_id: message.conversation_id
                    };
                } else if (item.type === 'text_file') {
                    messageObj = {
                        type: 'text_file',
                        filename: item.text_file.filename,
                        lines: item.text_file.lines,
                        is_bookmarked: message.is_bookmarked,
                        conversation_id: message.conversation_id
                    };
                } else if (item.type === 'image' && item.source.type === 'base64') {
                    messageObj = {
                        type: 'image_url',
                        url: item.source.data,
                        fullsize_url: item.source.data,
                        is_bookmarked: message.is_bookmarked,
                        conversation_id: message.conversation_id
                    };
                }

                addMessage(
                    message.type,
                    null,
                    { timestamp: timestamps, isNewMessage: false },
                    false,
                    messageObj,
                    prepend,
                    container,
                    message.id,
                    message.citations
                );
            });
        } else {
            messageObj = {
                type: 'text',
                text: String(parsedMessage),
                is_bookmarked: message.is_bookmarked,
                conversation_id: message.conversation_id
            };
            addMessage(
                message.type,
                null,
                { timestamp: timestamps, isNewMessage: false },
                false,
                messageObj,
                prepend,
                container,
                message.id,
                message.citations
            );
        }
    } catch (e) {
        messageObj = {
            type: 'text',
            text: String(message.message),
            is_bookmarked: message.is_bookmarked,
            conversation_id: message.conversation_id
        };
        addMessage(
            message.type,
            null,
            { timestamp: timestamps, isNewMessage: false },
            false,
            messageObj,
            prepend,
            container,
            message.id,
            message.citations
        );
    }

    // Mark the message as processed
    processedIds.add(message.id);
}

async function loadMessages(
    conversationId,
    prepend = false,
    targetMessageId = null,
    attempt = 0,
    viewGeneration = conversationViewGeneration
) {
    if (!isCurrentConversationView(conversationId, viewGeneration) ||
        isLoading || allMessagesLoaded) {
        return null;
    }

    const requestController = new AbortController();
    const loadState = {
        conversationId,
        generation: viewGeneration,
        controller: requestController
    };
    activeMessageLoad = loadState;
    currentAbortController = requestController;
    isLoading = true;
    disableInputControls();

    const chatWindow = document.getElementById('chat-window');
    const chatMessagesContainer = document.getElementById('chat-messages-container');
    let recurse = false;

    try {
        let url = `/api/conversations/${conversationId}/messages?limit=${limitMessage}`;
        if (oldestLoadedMessageId !== null && prepend) {
            url += `&before_id=${oldestLoadedMessageId}`;
        }

        const response = await secureFetch(url, {
            signal: requestController.signal
        });
        if (!response ||
            activeMessageLoad !== loadState ||
            !isCurrentConversationView(conversationId, viewGeneration)) {
            return null;
        }
        if (!response.ok) {
            throw new Error('Network response was not ok');
        }

        const data = await response.json();
        if (activeMessageLoad !== loadState ||
            !isCurrentConversationView(conversationId, viewGeneration)) {
            return null;
        }

        const messages = Array.isArray(data.messages) ? data.messages : [];
        const conversationInfo = data.conversation_info || {};
        setIncognitoUiState(isConversationIncognitoData(conversationInfo));
        setCurrentProviderHealth(conversationInfo.provider_health || null);
        allMessagesLoaded = !data.has_more;

        if (!prepend) {
            chatMessagesContainer.innerHTML = '';
        }

        const tempDiv = document.createElement('div');
        botname = conversationInfo.prompt_name;
        promptDescription = conversationInfo.prompt_description;
        botProfilePicture = conversationInfo.bot_profile_picture || '';
        botProfilePicture128 = conversationInfo.bot_profile_picture_128 || botProfilePicture;
        botProfilePictureFullsize = conversationInfo.bot_profile_picture_fullsize ||
            botProfilePicture128 || botProfilePicture;

        const sidebarEl = document.querySelector(`[data-conversation-id="${conversationId}"]`);
        if (sidebarEl) {
            sidebarEl.dataset.isPaid = conversationInfo.is_paid ? '1' : '0';
        }

        if (conversationInfo.extensions_enabled && window.extensionSelector) {
            window.extensionSelector.init(
                conversationInfo.extensions,
                conversationInfo.active_extension,
                conversationInfo.extensions_free_selection
            );
        } else if (window.extensionSelector) {
            window.extensionSelector.hide();
        }

        let targetMessageFound = false;
        messages.forEach(message => {
            processMessage(message, tempDiv, prepend);
            if (targetMessageId && message.id === targetMessageId) {
                targetMessageFound = true;
            }
        });

        if (!isCurrentConversationView(conversationId, viewGeneration)) {
            return null;
        }

        if (prepend) {
            const anchor = chatMessagesContainer.firstElementChild;
            const anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;
            chatMessagesContainer.insertBefore(tempDiv, chatMessagesContainer.firstChild);
            if (anchor) {
                const newAnchorTop = anchor.getBoundingClientRect().top;
                chatWindow.scrollTop += newAnchorTop - anchorTop;
            }
        } else {
            chatMessagesContainer.appendChild(tempDiv);
            chatWindow.scrollTop = chatWindow.scrollHeight;
        }

        if (messages.length > 0) {
            oldestLoadedMessageId = messages[0].id;
        }
        isCurrentConversationEmpty = messages.length === 0 && oldestLoadedMessageId === null;

        if (targetMessageId && !targetMessageFound && !allMessagesLoaded && attempt < 200) {
            recurse = true;
            releaseActiveMessageLoad(loadState, false);
            return loadMessages(
                conversationId,
                true,
                targetMessageId,
                attempt + 1,
                viewGeneration
            );
        }
        if (targetMessageId && targetMessageFound) {
            setTimeout(() => {
                if (isCurrentConversationView(conversationId, viewGeneration)) {
                    highlightAndScrollToMessage(targetMessageId);
                }
            }, 100);
        }
        return data;
    } catch (error) {
        if (error.name !== 'AbortError' && error.message !== 'Session expired' &&
            isCurrentConversationView(conversationId, viewGeneration)) {
            console.error('Error loading messages:', error);
        }
        return null;
    } finally {
        if (!recurse) {
            releaseActiveMessageLoad(loadState, true);
        }
    }
}

function refreshActiveConversation() {
    if (!currentConversationId) {
        return Promise.resolve();
    }

    const conversationId = currentConversationId;
    const viewGeneration = beginConversationViewTransition();

    // Reset pagination so the next fetch pulls fresh messages.
    oldestLoadedMessageId = null;
    allMessagesLoaded = false;
    processedMessageIds.clear();

    return loadMessages(conversationId, false, null, 0, viewGeneration);
}

window.refreshActiveConversation = refreshActiveConversation;

function getChatMessageElementById(messageId) {
    const chatMessagesContainer = document.getElementById('chat-messages-container');
    if (!chatMessagesContainer) return null;
    return chatMessagesContainer.querySelector(`.message[data-message-id="${messageId}"]`);
}

function highlightAndScrollToMessage(messageId) {
    const targetMessage = getChatMessageElementById(messageId);
    if (targetMessage) {
        targetMessage.scrollIntoView({ behavior: 'smooth', block: 'center' });
        // In search mode, use persistent highlight (removed when search clears)
        if (typeof messageSearchState !== 'undefined' && messageSearchState.active) {
            targetMessage.classList.add('highlight-persistent');
            if (typeof window.ensureSearchHighlightDismissButton === 'function') {
                window.ensureSearchHighlightDismissButton(targetMessage);
            }
        } else {
            targetMessage.classList.add('highlight');
            setTimeout(() => {
                targetMessage.classList.remove('highlight');
            }, 2000);
        }
    } else {
        console.error('Message with specified ID not found:', messageId);
    }
}

function enableInputControls() {
    // Don't enable if conversation is locked
    if (isCurrentConversationLocked) {
        document.getElementById('loading-indicator').style.display = 'none';
        return;
    }

    document.getElementById('message-text').disabled = false;
    document.querySelector('#form-message button[type="submit"]').disabled = false;
    if (Config.can_send_files) {
        document.getElementById('chat-files').disabled = false;
    }
    const plusBtn = document.getElementById('plus-menu-btn');
    if (plusBtn) plusBtn.disabled = false;
    document.getElementById('loading-indicator').style.display = 'none';
}

function disableInputControls() {
    document.querySelector('#form-message button[type="submit"]').disabled = true;
    document.getElementById('chat-files').disabled = true;
    const plusBtn = document.getElementById('plus-menu-btn');
    if (plusBtn) plusBtn.disabled = true;
    closePlusMenu();
    document.getElementById('loading-indicator').style.display = 'block';
}


function setupInfiniteScroll(conversationId, viewGeneration = conversationViewGeneration) {
    const chatWindow = document.getElementById('chat-window');
    chatWindow.onscroll = function() {
        if (!isCurrentConversationView(conversationId, viewGeneration)) {
            return;
        }
        // Infinite scroll: load older messages when at top
        if (chatWindow.scrollTop <= 1 && !isLoading && !allMessagesLoaded) {
            loadMessages(conversationId, true, null, 0, viewGeneration);
        }

        // Auto-scroll management: detect user scroll intent
        if (isNearBottom(chatWindow)) {
            isUserScrolledUp = false;
        } else {
            isUserScrolledUp = true;
        }

        updateScrollBottomBtn();
    };
}

let isCurrentConversationEmpty = true;
let isFirstCall = true;

function isConversationIncognitoData(conversationData, selectedChat = null) {
    if (conversationData && (
        conversationData.is_incognito === true ||
        conversationData.hidden_from_history === true ||
        conversationData.purge_on_close === true
    )) {
        return true;
    }
    return selectedChat && selectedChat.dataset.isIncognito === 'true';
}

function setIncognitoUiState(isIncognito) {
    currentConversationIncognito = Boolean(isIncognito);
    const badge = document.getElementById('incognito-chat-badge');
    const closeBtn = document.getElementById('close-incognito-chat-btn');
    if (badge) {
        badge.hidden = !currentConversationIncognito;
    }
    if (closeBtn) {
        closeBtn.hidden = !currentConversationIncognito;
    }
}

function closeCurrentIncognitoConversation() {
    if (!currentConversationIncognito || !currentConversationId) {
        return Promise.resolve(false);
    }
    const closingId = currentConversationId;
    return secureFetch(`/api/conversations/${closingId}/incognito/close`, {
        method: 'POST'
    })
    .then(response => response ? response.json() : null)
    .then(() => {
        removeConversationElement(closingId);
        loadedConversationIds.delete(Number(closingId));
        loadedConversationIds.delete(String(closingId));
        if (String(currentConversationId) === String(closingId)) {
            currentConversationId = null;
            localStorage.removeItem('activeConversationId');
            setIncognitoUiState(false);
            const chatMessagesContainer = document.getElementById('chat-messages-container');
            if (chatMessagesContainer) {
                chatMessagesContainer.innerHTML = '';
            }
        }
        return true;
    })
    .catch(error => {
        console.error('Error closing incognito conversation:', error);
        NotificationModal.error('Close Failed', 'Could not close the incognito chat.');
        return false;
    });
}

function maybeCloseCurrentIncognitoBeforeLeaving() {
    if (!currentConversationIncognito || !currentConversationId) {
        return Promise.resolve(true);
    }
    return closeCurrentIncognitoConversation().then(result => result !== false);
}

function initIncognitoChatControls() {
    const newIncognitoBtn = document.getElementById('new-incognito-chat-btn');
    if (newIncognitoBtn) {
        newIncognitoBtn.addEventListener('click', function(e) {
            e.preventDefault();
            const dropdownToggle = document.querySelector('.btn-group .dropdown-toggle-split[data-bs-toggle="dropdown"]');
            if (dropdownToggle && typeof bootstrap !== 'undefined') {
                bootstrap.Dropdown.getOrCreateInstance(dropdownToggle).hide();
            }
            SessionManager.validateSession(true).then(isValid => {
                if (isValid) {
                    startNewConversation(null, { incognito: true });
                }
            });
        });
    }

    const closeBtn = document.getElementById('close-incognito-chat-btn');
    if (closeBtn) {
        closeBtn.addEventListener('click', function(e) {
            e.preventDefault();
            closeCurrentIncognitoConversation().then(closed => {
                if (closed) {
                    startNewConversation();
                }
            });
        });
    }

    window.addEventListener('pagehide', function() {
        if (!currentConversationIncognito || !currentConversationId) return;
        const url = `/api/conversations/${currentConversationId}/incognito/close`;
        if (navigator.sendBeacon) {
            navigator.sendBeacon(url, new Blob([], { type: 'application/json' }));
        } else {
            fetch(url, { method: 'POST', keepalive: true }).catch(() => {});
        }
        localStorage.removeItem('activeConversationId');
    });
}

function getSelectedNewChatLlmId(options = {}) {
    options = options || {};
    const explicitLlmId = options.llm_id !== undefined ? options.llm_id : options.llmId;
    const rawValue = explicitLlmId !== undefined && explicitLlmId !== null && explicitLlmId !== ''
        ? explicitLlmId
        : document.getElementById('llmDropdown')?.value;
    const parsed = parseInt(rawValue, 10);
    return isNaN(parsed) || parsed <= 0 ? null : parsed;
}

window.getSelectedNewChatLlmId = getSelectedNewChatLlmId;

function activeConversationHasExternalAccess() {
    const selected = window.selectedChat &&
        conversationIdsMatch(
            window.selectedChat.dataset?.conversationId,
            currentConversationId
        )
        ? window.selectedChat
        : document.querySelector(
            `.active-chat[data-conversation-id="${currentConversationId}"]`
        );
    if (!selected) return false;
    const data = selected._conversationData || {};
    const bindingCount = parseInt(data.external_bindings?.effective_count || 0, 10);
    return Boolean(
        selected.dataset?.externalPlatform ||
        data.external_platform ||
        (Number.isInteger(bindingCount) && bindingCount > 0) ||
        selected.classList?.contains('external-device-row')
    );
}

window.activeConversationHasExternalAccess = activeConversationHasExternalAccess;

function getActiveConversationPromptId() {
    const selected = window.selectedChat &&
        conversationIdsMatch(
            window.selectedChat.dataset?.conversationId,
            currentConversationId
        )
        ? window.selectedChat
        : document.querySelector(
            `.active-chat[data-conversation-id="${currentConversationId}"]`
        );
    const rawPromptId = selected?.dataset?.promptId ||
        selected?._conversationData?.prompt_id;
    const parsedPromptId = parseInt(rawPromptId, 10);
    return Number.isInteger(parsedPromptId) && parsedPromptId > 0
        ? parsedPromptId
        : null;
}

window.getActiveConversationPromptId = getActiveConversationPromptId;

function startNewConversation(promptId = null, options = {}) {
    if (admin_view) {
        return Promise.resolve();
    }
    const incognito = options && options.incognito === true;
    const forceCreate = options && options.__forceCreate === true;
    const activeFolderId = typeof window.getActiveConversationFolderId === 'function'
        ? window.getActiveConversationFolderId()
        : null;
    const reuseLocationMatches = activeFolderId === null;
    const selectedPromptId = parseInt(
        document.getElementById('promptDropdown')?.value,
        10
    );
    const reuseContextMatches = promptId === null &&
        Number.isInteger(selectedPromptId) &&
        selectedPromptId === getActiveConversationPromptId() &&
        !currentConversationIncognito &&
        !activeConversationHasExternalAccess();

    if (!forceCreate && reuseLocationMatches && reuseContextMatches &&
        !incognito && !isFirstCall && isCurrentConversationEmpty) {
        if (window.modelSelector?.reconcileDefaultForEmptyConversation &&
            currentConversationId) {
            const reuseConversationId = currentConversationId;
            const reuseViewGeneration = conversationViewGeneration;
            return window.modelSelector.reconcileDefaultForEmptyConversation(
                reuseConversationId,
                reuseViewGeneration
            ).then(reused => {
                if (reused) return true;
                if (!isCurrentConversationView(
                    reuseConversationId,
                    reuseViewGeneration
                )) return false;
                return startNewConversation(promptId, {
                    ...options,
                    __forceCreate: true
                });
            });
        }
        return startNewConversation(promptId, {
            ...options,
            __forceCreate: true
        });
    }

    hideScrollNavButtons();

    // If promptId is not passed, get it from the dropdown
    if (promptId === null) {
        const promptDropdown = document.getElementById('promptDropdown');
        if (promptDropdown) {
            promptId = promptDropdown.value;
        }
    }
    
    let body = {};
    if (promptId !== null) {
        body.prompt_id = promptId;
    }
    if (incognito) {
        body.incognito = true;
    }
    const llmId = getSelectedNewChatLlmId(options);
    if (llmId !== null) {
        body.llm_id = llmId;
    }
    const requestViewGeneration = conversationViewGeneration;

    return maybeCloseCurrentIncognitoBeforeLeaving().then(canContinue => {
        if (!canContinue) {
            return Promise.resolve();
        }
        return secureFetch('/api/conversations/new', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(body)
        });
    })
    .then(response => {
        if (!response) {
            // secureFetch returned null (likely session expired)
            throw new Error('Session expired');
        }
        return response.json();
    })
    .then(data => {
        if (requestViewGeneration !== conversationViewGeneration) {
            return null;
        }
        startDate = new Date(); 
        if (!data.hidden_from_history && !data.is_incognito) {
            addConversationElement(data, data.name, null, true);
        }
        return continueConversation(data.id, data.name, data.machine, false, null, data);
    })
    .then(result => {
        if (result === null ||
            conversationViewGeneration !== requestViewGeneration + 1) {
            return;
        }
        isCurrentConversationEmpty = true; 
        isFirstCall = false;
    })
    .catch(error => {
        if (error.message === 'Session expired') {
            // Session validation was already handled by secureFetch, no need to log as error
            return;
        }
        console.error('Error starting a new conversation:', error);
    });
}

function stopReceivingStream(event) {
    if (event) {
        event.preventDefault();
    }
    userStopped = true;
    if (controller) {
        try {
            controller.abort();
        } catch (err) {
            console.warn('Could not abort active request:', err);
        }
    }

    fetch(`/api/conversations/${currentConversationId}/stop`, {
        method: 'POST'
    }).then(response => {
        if (response.ok) {
        } else {
            console.error('Server failed to acknowledge stop request.');
        }
    }).catch(error => {
        console.error('Error sending stop request:', error);
    });

    toggleSendButton();
    removeLoadingIndicator();
}

function toggleBookmark(messageId, conversationId, bookmarkIcon) {
    const isCurrentlyBookmarked = bookmarkIcon.classList.contains('bookmarked');
    const action = isCurrentlyBookmarked ? 'remove' : 'add';

    // Get the correct conversationId from the data-conversationId attribute of the icon
    const correctConversationId = bookmarkIcon.dataset.bookmarkConversationId || conversationId;

    fetch(`/api/conversations/${correctConversationId}/bookmark`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ 
            message_id: messageId,
            action: action 
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            if (action === 'add') {
                bookmarkIcon.classList.add('fa-check');
                setTimeout(() => {
                    bookmarkIcon.classList.remove('fa-check');
                    bookmarkIcon.classList.add('bookmarked');
                    bookmarkIcon.style.display = 'inline';
                }, 1000);
            } else {
                bookmarkIcon.classList.add('fade-out');
                setTimeout(() => {
                    bookmarkIcon.classList.remove('bookmarked');
                    bookmarkIcon.classList.remove('fade-out');
                    bookmarkIcon.style.display = 'none';
                    
                    if (document.querySelector('.chatbot-info h4').textContent === "My Bookmarks") {
                        bookmarkIcon.closest('.message').remove();
                    }
                }, 300);
            }
        } else {
            console.error(`Error ${action === 'add' ? 'adding' : 'removing'} message ${action === 'add' ? 'to' : 'from'} favorites:`, data.error);
        }
    })
    .catch(error => {
        console.error(`Error ${action === 'add' ? 'adding' : 'removing'} message ${action === 'add' ? 'to' : 'from'} favorites:`, error);
    });
}

function loadBookmarkedMessages() {
    const myBookmarksBtn = document.getElementById('my-bookmarks-btn');
    if (myBookmarksBtn.classList.contains('active-bookmarks')) {
        return Promise.resolve();
    }
    if (activeBookmarksLoad) {
        return activeBookmarksLoad.promise;
    }

    const viewGeneration = beginConversationViewTransition();
    currentConversationId = null;
    const bookmarksController = new AbortController();
    const loadState = {
        generation: viewGeneration,
        controller: bookmarksController,
        promise: null
    };
    activeBookmarksLoad = loadState;

    const isActiveBookmarksRequest = () => (
        activeBookmarksLoad === loadState &&
        conversationViewGeneration === viewGeneration &&
        !bookmarksController.signal.aborted
    );

    loadState.promise = secureFetch('/api/bookmarks', {
        signal: bookmarksController.signal
    })
    .then(response => {
        if (!response || !isActiveBookmarksRequest()) return null;
        if (!response.ok) throw new Error('Could not load bookmarks');
        return response.json();
    })
    .then(messages => {
        if (!Array.isArray(messages) || !isActiveBookmarksRequest()) return;

        const chatMessagesContainer = document.getElementById('chat-messages-container');
        chatMessagesContainer.innerHTML = '';
        const localProcessedMessageIds = new Set();
        
        const chatTitle = document.querySelector('.chatbot-info h4');
        const chatModel = document.getElementById('chat-model');
        chatTitle.textContent = "My Bookmarks";
        chatTitle.title = '';
        chatModel.textContent = '';

        // Replace avatar with bookmark icon
        const chatTitleAvatar = document.getElementById('chat-title-avatar');
        chatTitleAvatar.innerHTML = '<i class="fas fa-bookmark" style="font-size:1.4rem;opacity:0.7"></i>';

        // Hide model selector and extension selector (not applicable)
        const modelSelectorContainer = document.querySelector('.model-selector-container');
        if (modelSelectorContainer) modelSelectorContainer.style.display = 'none';
        const extensionSelector = document.getElementById('extension-selector-container');
        if (extensionSelector) extensionSelector.style.display = 'none';
        
        // Group messages by conversation (local variable)
        let localConversationId = null;
        messages.forEach(message => {
            if (message.conversation_id !== localConversationId) {
                localConversationId = message.conversation_id;
                const header = document.createElement('div');
                header.className = 'bookmark-conversation-header';
                header.title = 'Click to go to conversation';
                const name = document.createElement('span');
                name.className = 'bookmark-conversation-name';
                name.textContent = message.chat_name || `Chat ${message.conversation_id}`;
                header.appendChild(name);
                header.addEventListener('click', () => {
                    continueConversation(message.conversation_id, message.chat_name);
                });
                chatMessagesContainer.appendChild(header);
            }
            processMessage(
                message,
                chatMessagesContainer,
                false,
                localProcessedMessageIds
            );
        });

        // Remove all rollback icons
        document.querySelectorAll('.rollback-icon').forEach(icon => icon.remove());

        document.getElementById('message-input-container').style.display = 'none';
        const lockedBanner = document.getElementById('locked-conversation-banner');
        if (lockedBanner) lockedBanner.style.display = 'none';

        // Change the active chat to previously-active-chat
        const activeChat = document.querySelector('.list-group-item-action.active-chat');
        if (activeChat) {
            activeChat.classList.remove('active-chat');
            activeChat.classList.add('previously-active-chat');
        }
        
        // Deactivate other chats, but keep the previously-active-chat
        document.querySelectorAll('.list-group-item-action').forEach(function(item) {
            if (!item.classList.contains('previously-active-chat')) {
                item.classList.remove('active-chat', 'active-bookmarks');
            }
        });
        
        // Activate My Bookmarks button
        myBookmarksBtn.classList.add('active-bookmarks');
    })
    .catch(error => {
        if (error.name !== 'AbortError' && isActiveBookmarksRequest()) {
            console.error('Error loading bookmarked messages:', error);
        }
    })
    .finally(() => {
        if (activeBookmarksLoad === loadState) {
            activeBookmarksLoad = null;
        }
    });

    return loadState.promise;
}

const myBookmarksButton = document.getElementById('my-bookmarks-btn');
if (myBookmarksButton) {
    myBookmarksButton.addEventListener('click', function(e) {
        e.preventDefault();
        loadBookmarkedMessages();
    });
}

/* Scroll navigation buttons */

function navScrollToTop() {
    const chatWindow = document.getElementById('chat-window');
    chatWindow.scrollTo({ top: 0, behavior: 'smooth' });
}

function navScrollToBottom() {
    const chatWindow = document.getElementById('chat-window');
    chatWindow.scrollTo({ top: chatWindow.scrollHeight, behavior: 'smooth' });
}

function updateScrollBottomBtn() {
    const btn = document.getElementById('scroll-bottom-btn');
    if (!btn) return;
    btn.classList.toggle('visible', isUserScrolledUp);
}

function hideScrollNavButtons() {
    document.getElementById('scroll-top-btn')?.classList.remove('visible');
    document.getElementById('scroll-bottom-btn')?.classList.remove('visible');
}

// Scroll-to-top: show on mouse hover in upper zone of chat area
(function initScrollTopHover() {
    const windowChat = document.getElementById('window-chat');
    const btn = document.getElementById('scroll-top-btn');
    if (!windowChat || !btn) return;

    const HOVER_ZONE_HEIGHT = 120;

    windowChat.addEventListener('mousemove', function(e) {
        const rect = windowChat.getBoundingClientRect();
        const relativeY = e.clientY - rect.top;
        btn.classList.toggle('visible', relativeY <= HOVER_ZONE_HEIGHT);
    });

    windowChat.addEventListener('mouseleave', function() {
        btn.classList.remove('visible');
    });
})();

document.getElementById('scroll-top-btn')?.addEventListener('click', navScrollToTop);
document.getElementById('scroll-bottom-btn')?.addEventListener('click', navScrollToBottom);

// Dynamic layout positioning via CSS custom properties on :root
// Used by scroll-nav buttons and sidebar-user alignment
(function initScrollNavPositioning() {
    const header = document.querySelector('.chatbot-info');
    const inputBar = document.getElementById('message-input-container');
    if (!header || !inputBar) return;

    const root = document.documentElement;
    const observer = new ResizeObserver(entries => {
        for (const entry of entries) {
            if (entry.target === header) {
                root.style.setProperty('--chat-header-h', entry.target.offsetHeight + 'px');
            } else if (entry.target === inputBar) {
                root.style.setProperty('--chat-input-h', entry.target.offsetHeight + 'px');
            }
        }
    });

    observer.observe(header);
    observer.observe(inputBar);
})();

async function handleResponse(response) {
    removeLoadingIndicator();
    switch (response.status) {
        case 402:
            showInsufficientBalancePopup("transcribe audio");
            break;
        case 204:
            break;
        case 500:
            const data = await response.json();
            NotificationModal.error('Server Error', data.error);
            break;
        default:
            if (response.ok) {
                const data = await response.json();
                if (data["prompt"]) {
                    document.getElementById('message-text').value = data["prompt"];
                    document.getElementById('send-button').click();
                }
            } else {
            }
            break;
    }
}

function showPromptInfo() {
    const chatMessagesContainer = document.getElementById('chat-messages-container');
    const existingPromptInfo = chatMessagesContainer.querySelector('.prompt-info');

    if (existingPromptInfo) {
        existingPromptInfo.remove();
    }

    const promptInfo = document.createElement('div');
    promptInfo.classList.add('prompt-info');

    const infoContainer = document.createElement('div');
    infoContainer.classList.add('prompt-info-container');

    const imageSection = document.createElement('div');
    imageSection.classList.add('prompt-image-section');
    imageSection.style.position = 'relative';

    const initialContainer = document.createElement('div');
    initialContainer.classList.add('prompt-initial');
    const initial = (botname || 'Assistant').charAt(0).toUpperCase();
    initialContainer.textContent = initial;
    initialContainer.title = botname || 'Assistant';
    imageSection.appendChild(initialContainer);

    const displayAvatarUrl = (
        typeof botProfilePicture128 !== 'undefined' && botProfilePicture128
    ) || (
        typeof botProfilePicture !== 'undefined' && botProfilePicture
    ) || '';
    const fullsizeAvatarUrl = (
        typeof botProfilePictureFullsize !== 'undefined' && botProfilePictureFullsize
    ) || displayAvatarUrl;

    if (displayAvatarUrl) {
        const img = document.createElement('img');
        img.src = displayAvatarUrl;
        img.style.position = 'absolute';
        img.style.top = '0';
        img.style.left = '0';
        img.style.width = '100%';
        img.style.height = '100%';
        img.style.objectFit = 'cover';

        img.style.cursor = 'pointer';
        img.dataset.fullsize = fullsizeAvatarUrl;
        img.onclick = function() {
            imageHandler.showFullsize(this.dataset.fullsize, null);
        };

        imageSection.appendChild(img);
    }
    
    const textSection = document.createElement('div');
    textSection.classList.add('prompt-text-section');
    
    const promptName = document.createElement('h3');
    promptName.classList.add('prompt-name');
    promptName.textContent = botname || 'Assistant';
    
    textSection.appendChild(promptName);

	const description = document.createElement('p');
	description.classList.add('prompt-description');
	description.textContent = promptDescription;
	textSection.appendChild(description);

    // Add extension level pills if available
    if (window.extensionSelector && window.extensionSelector.extensions.length > 0) {
        const pillsDiv = document.createElement('div');
        pillsDiv.className = 'extension-pills-row mt-2';
        window.extensionSelector.extensions.forEach(ext => {
            const isActive = ext.id === window.extensionSelector.currentExtensionId;
            const pill = document.createElement('span');
            pill.className = 'extension-pill';
            if (isActive) pill.classList.add('active');
            pill.textContent = String(ext.name || '');
            pillsDiv.appendChild(pill);
        });
        textSection.appendChild(pillsDiv);
    }

    infoContainer.appendChild(imageSection);
    infoContainer.appendChild(textSection);
    promptInfo.appendChild(infoContainer);

    if (chatMessagesContainer.firstChild) {
        chatMessagesContainer.insertBefore(promptInfo, chatMessagesContainer.firstChild);
    } else {
        chatMessagesContainer.appendChild(promptInfo);
    }
}

// Model Selector functionality
class ModelSelector {
    constructor() {
        this.dropdownMenu = document.getElementById('model-dropdown-menu');
        this.dropdownIcon = document.getElementById('model-dropdown-icon');
        this.modelSelectorContainer = document.querySelector('.model-selector-container');
        this.dropdownContent = document.getElementById('model-dropdown-content');
        this.chatModel = document.getElementById('chat-model');
        this.currentModel = null;
        this.currentLlmId = null;
        this.forcedLlmId = null;
        this.hideLlmName = false;
        this.allowedLlms = null;
        this.requestGeneration = 0;
        this.identityRevision = 0;
        this.conversationIdentityRevisions = new Map();


        if (this.dropdownMenu) {
            // Initialize dropdown menu
        }
        
        this.init();
    }
    
    init() {
        // Initialize event listeners
        if (this.modelSelectorContainer) {
            this.modelSelectorContainer.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleDropdown();
            });
        }
        
        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!this.dropdownMenu.contains(e.target) && !this.modelSelectorContainer.contains(e.target)) {
                this.closeDropdown();
            }
        });
        
        // Populate models on page load
        this.populateModels();
    }
    
    populateModels(filterIds = null) {

        if (!window.availableModels || !this.dropdownContent) {
            return;
        }

        const models = filterIds
            ? window.availableModels.filter(m => filterIds.includes(m.id))
            : window.availableModels;

        // Group models by machine
        const groupedModels = {};
        models.forEach(model => {
            if (!groupedModels[model.machine]) {
                groupedModels[model.machine] = [];
            }
            groupedModels[model.machine].push(model);
        });
        
        
        let html = '';
        
        // Sort machines: put GPT first, then alphabetically
        const sortedMachines = Object.keys(groupedModels).sort((a, b) => {
            if (a === 'GPT') return -1;
            if (b === 'GPT') return 1;
            return a.localeCompare(b);
        });
        
        sortedMachines.forEach((machine, groupIndex) => {
            if (groupIndex > 0) {
                html += '<div style="height: 4px;"></div>'; // Separator
            }
            
            const safeMachine = escapeHtml(machine);
            html += `<div class="model-group">`;
            html += `<div class="model-group-header">${safeMachine}</div>`;
            
            // Sort models within each group
            const sortedModels = groupedModels[machine].sort((a, b) => (a.display_name || a.model).localeCompare(b.display_name || b.model));
            
            sortedModels.forEach(model => {
                const displayText = escapeHtml(String(model.display_name || model.model || ''));
                const safeModel = escapeHtml(String(model.model || ''));
                html += `
                    <div class="model-item" data-llm-id="${model.id}" data-model="${safeModel}">
                        <span>${displayText}</span>
                    </div>
                `;
            });
            
            html += '</div>';
        });
        
        this.dropdownContent.innerHTML = html;
        
        
        // Add click listeners to model items
        this.dropdownContent.querySelectorAll('.model-item').forEach(item => {
            item.addEventListener('click', (e) => {
                e.stopPropagation();
                const llmId = parseInt(item.dataset.llmId, 10);
                const modelName = item.dataset.model;
                this.selectModel(llmId, modelName);
            });
        });
    }
    
    updateCurrentModel(modelName, llmId = null) {
        this.identityRevision = (Number.isInteger(this.identityRevision)
            ? this.identityRevision
            : 0) + 1;
        this.currentModel = modelName;

        const parsedLlmId = parseInt(llmId, 10);
        if (Number.isInteger(parsedLlmId) && parsedLlmId > 0) {
            // Preserve the backend identity even if the row is no longer in the
            // user's current selector (for example after unlink). That lets a
            // legacy conversation switch away from a now-hidden GPTSub row.
            this.currentLlmId = parsedLlmId;
            window.conversationModelIdentityUnknown = false;
        } else {
            // Provider identity cannot be reconstructed from a model string.
            // Keep this view fail-closed until /details supplies the exact id.
            this.currentLlmId = null;
            window.conversationModelIdentityUnknown = true;
        }
        
        // Update UI to show current model
        this.updateModelDisplay();
        if (typeof ApiKeyManager !== 'undefined') {
            ApiKeyManager.updateUI();
        }
        if (typeof window.updateConversationBalanceAvailability === 'function') {
            window.updateConversationBalanceAvailability();
        }
        
        // Update thinking tokens visibility
        if (window.updateThinkingTokensVisibility) {
            setTimeout(() => {
                window.updateThinkingTokensVisibility();
            }, 100);
        }
    }
    
    updateModelDisplay() {
        // Update current model highlighting in dropdown
        this.dropdownContent.querySelectorAll('.model-item').forEach(item => {
            item.classList.remove('current');
            if (this.currentLlmId !== null &&
                Number(item.dataset.llmId) === Number(this.currentLlmId)) {
                item.classList.add('current');
            }
        });
    }

    cancelPendingRequest() {
        this.requestGeneration += 1;
        if (typeof window.invalidateConversationModelMutations === 'function') {
            window.invalidateConversationModelMutations();
        }
    }

    isRequestCurrent(state) {
        return this.requestGeneration === state.requestGeneration &&
            isCurrentConversationView(state.conversationId, state.viewGeneration);
    }

    scheduleMutation(task) {
        if (typeof window.enqueueConversationModelMutation === 'function') {
            return window.enqueueConversationModelMutation(task);
        }
        return Promise.resolve().then(task).then(
            value => ({ value, error: null, isLatest: true }),
            error => ({ value: null, error, isLatest: true })
        );
    }

    async requestModelUpdate(state, onlyIfEmpty = false) {
        const response = await fetch(`/api/conversations/${state.conversationId}/model`, {
            method: 'PATCH',
            headers: {
                'Content-Type': 'application/json',
            },
            credentials: 'include',
            body: JSON.stringify({
                llm_id: state.llmId,
                only_if_empty: onlyIfEmpty
            })
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(result.detail || 'Failed to update model');
        }
        if (!result.success) {
            throw new Error('Failed to update model');
        }
        return result;
    }

    applyCommittedModel(
        llmId,
        modelName,
        conversationId = currentConversationId,
        viewGeneration = conversationViewGeneration
    ) {
        const parsedLlmId = parseInt(llmId, 10);
        if (!Number.isInteger(parsedLlmId) || parsedLlmId <= 0) {
            return false;
        }

        this.cacheCommittedModel(conversationId, parsedLlmId, modelName);
        if (!isCurrentConversationView(conversationId, viewGeneration)) {
            return false;
        }

        this.currentModel = modelName;
        this.currentLlmId = parsedLlmId;
        this.identityRevision = (Number.isInteger(this.identityRevision)
            ? this.identityRevision
            : 0) + 1;
        window.conversationModelIdentityUnknown = false;

        const modelData = (window.availableModels || []).find(
            model => Number(model.id) === parsedLlmId
        );
        this.chatModel.textContent = this.hideLlmName
            ? 'AI'
            : (modelData?.display_name || modelName);
        this.updateModelDisplay();
        if (typeof ApiKeyManager !== 'undefined') {
            ApiKeyManager.updateUI();
        }
        if (typeof window.updateConversationBalanceAvailability === 'function') {
            window.updateConversationBalanceAvailability();
        }
        if (window.updateThinkingTokensVisibility) {
            window.updateThinkingTokensVisibility();
        }
        return true;
    }

    cacheCommittedModel(conversationId, llmId, modelName) {
        this.bumpConversationIdentityRevision(conversationId);
        document.querySelectorAll(
            `[data-conversation-id="${conversationId}"]`
        ).forEach(element => {
            element.dataset.llmId = String(llmId);
            element.dataset.llmModel = modelName;
            if (element._conversationData) {
                element._conversationData.llm_id = Number(llmId);
                element._conversationData.llm_model = modelName;
            }
        });
    }

    invalidateCachedModel(conversationId) {
        this.bumpConversationIdentityRevision(conversationId);
        document.querySelectorAll(
            `[data-conversation-id="${conversationId}"]`
        ).forEach(element => {
            delete element.dataset.llmId;
            delete element.dataset.llmModel;
            if (element._conversationData) {
                delete element._conversationData.llm_id;
                delete element._conversationData.llm_model;
            }
        });
    }

    async reconcileConversationModelIdentity(conversationId) {
        const identityRevision = this.getConversationIdentityRevision(
            conversationId
        );
        try {
            const response = await fetch(
                `/api/conversations/${conversationId}/details`,
                { credentials: 'include' }
            );
            if (!response.ok) throw new Error('model identity refresh failed');
            const details = await response.json();
            const parsedLlmId = parseInt(details.llm_id, 10);
            if (!Number.isInteger(parsedLlmId) || parsedLlmId <= 0 ||
                typeof details.model !== 'string' || !details.model) {
                throw new Error('model identity refresh was malformed');
            }
            if (this.getConversationIdentityRevision(conversationId) !==
                identityRevision) {
                return false;
            }
            this.cacheCommittedModel(conversationId, parsedLlmId, details.model);
            if (conversationIdsMatch(currentConversationId, conversationId)) {
                this.applyCommittedModel(
                    parsedLlmId,
                    details.model,
                    conversationId,
                    conversationViewGeneration
                );
            }
            return true;
        } catch (_error) {
            if (conversationIdsMatch(currentConversationId, conversationId)) {
                window.conversationModelIdentityUnknown = true;
            }
            return false;
        }
    }

    getConversationIdentityRevision(conversationId) {
        if (!this.conversationIdentityRevisions ||
            typeof this.conversationIdentityRevisions.get !== 'function') {
            this.conversationIdentityRevisions = new Map();
        }
        return this.conversationIdentityRevisions.get(String(conversationId)) || 0;
    }

    bumpConversationIdentityRevision(conversationId) {
        const key = String(conversationId);
        const next = this.getConversationIdentityRevision(key) + 1;
        this.conversationIdentityRevisions.set(key, next);
        return next;
    }

    async requestAndCacheModelUpdate(state, onlyIfEmpty = false) {
        try {
            const result = await this.requestModelUpdate(state, onlyIfEmpty);
            if (result?.updated) {
                const committedLlmId = result.llm_id || state.llmId;
                const committedModel = result.model || state.modelName;
                this.cacheCommittedModel(
                    state.conversationId,
                    committedLlmId,
                    committedModel
                );
                if (conversationIdsMatch(
                    currentConversationId,
                    state.conversationId
                )) {
                    this.applyCommittedModel(
                        committedLlmId,
                        committedModel,
                        state.conversationId,
                        conversationViewGeneration
                    );
                }
            }
            return result;
        } catch (error) {
            // A transport error may arrive after the server committed. Keep the
            // send barrier until /details either restores an exact identity or
            // latches this view fail-closed.
            this.invalidateCachedModel(state.conversationId);
            await this.reconcileConversationModelIdentity(state.conversationId);
            throw error;
        }
    }

    selectModel(llmId, modelName, options = {}) {
        const parsedLlmId = parseInt(llmId, 10);
        if (!currentConversationId || !Number.isInteger(parsedLlmId) || parsedLlmId <= 0) {
            this.closeDropdown();
            return Promise.resolve(false);
        }
        if (document.getElementById('send-button')?.innerText === 'Stop') {
            NotificationModal.warning(
                'Message in progress',
                'Wait for the current message to finish before changing the AI model.'
            );
            this.closeDropdown();
            return Promise.resolve(false);
        }

        const state = {
            conversationId: currentConversationId,
            viewGeneration: conversationViewGeneration,
            requestGeneration: ++this.requestGeneration,
            llmId: parsedLlmId,
            modelName,
            previousModel: this.currentModel,
            previousLlmId: this.currentLlmId
        };
        this.chatModel.textContent = 'Updating...';

        return this.scheduleMutation(() => this.requestAndCacheModelUpdate(
            state,
            options.onlyIfEmpty === true
        )).then(async ({ value, error, isLatest }) => {
            if (error) {
                if (!isLatest || !this.isRequestCurrent(state)) return false;
                console.error('Error updating model:', error);
                this.showError(error.message);
                this.closeDropdown();
                return false;
            }
            if (!isLatest || !this.isRequestCurrent(state)) {
                return false;
            }
            if (!value?.updated) {
                this.closeDropdown();
                return false;
            }

            const applied = this.applyCommittedModel(
                value.llm_id || parsedLlmId,
                value.model || modelName,
                state.conversationId,
                state.viewGeneration
            );
            if (applied) this.showSuccess();
            this.closeDropdown();
            return applied;
        });
    }

    reconcileDefaultForEmptyConversation(conversationId, viewGeneration) {
        if (typeof isCurrentConversationEmpty === 'undefined' ||
            isCurrentConversationEmpty !== true ||
            !isCurrentConversationView(conversationId, viewGeneration) ||
            this.forcedLlmId) {
            return Promise.resolve(false);
        }
        const defaultDropdown = document.getElementById('llmDropdown');
        const defaultLlmId = parseInt(defaultDropdown?.value, 10);
        if (!Number.isInteger(defaultLlmId) || defaultLlmId <= 0 ||
            (this.allowedLlms && !this.allowedLlms.map(Number).includes(defaultLlmId))) {
            return Promise.resolve(false);
        }
        const model = (window.availableModels || []).find(
            item => Number(item.id) === defaultLlmId
        );
        if (!model) return Promise.resolve(false);
        return this.selectModel(defaultLlmId, model.model, { onlyIfEmpty: true });
    }
    
    showSuccess() {
        // Briefly highlight the model selector
        this.modelSelectorContainer.style.backgroundColor = 'rgba(139, 128, 116, 0.2)';
        setTimeout(() => {
            this.modelSelectorContainer.style.backgroundColor = '';
        }, 600);
    }
    
    showError(message = null) {
        // Briefly show error state
        this.modelSelectorContainer.style.backgroundColor = 'rgba(244, 67, 54, 0.1)';
        this.chatModel.style.color = '#f44336';

        // Show error tooltip if message provided
        if (message) {
            const tooltip = document.createElement('div');
            tooltip.className = 'model-error-tooltip';
            tooltip.textContent = message;
            tooltip.style.cssText = 'position:absolute;top:100%;left:50%;transform:translateX(-50%);background:#f44336;color:#fff;padding:4px 10px;border-radius:4px;font-size:0.8rem;white-space:nowrap;z-index:1000;margin-top:4px;';
            this.modelSelectorContainer.style.position = 'relative';
            this.modelSelectorContainer.appendChild(tooltip);
            setTimeout(() => tooltip.remove(), 3000);
        }

        setTimeout(() => {
            this.modelSelectorContainer.style.backgroundColor = '';
            this.chatModel.style.color = '';
        }, 2000);
    }
    
    toggleDropdown() {
        if (this.dropdownMenu.classList.contains('show')) {
            this.closeDropdown();
        } else {
            this.openDropdown();
        }
    }
    
    openDropdown() {
        // Don't open if model is forced
        if (this.forcedLlmId) {
            return;
        }

        // Don't open if no conversation is active
        if (!currentConversationId) {
            return;
        }
        
        this.dropdownMenu.classList.add('show');
        this.dropdownIcon.classList.add('expanded');
        
        
        
        this.updateModelDisplay(); // Ensure current model is highlighted
    }
    
    closeDropdown() {
        this.dropdownMenu.classList.remove('show');
        this.dropdownIcon.classList.remove('expanded');
    }

    applyRestrictions(forcedLlmId, hideLlmName, allowedLlms) {
        this.forcedLlmId = forcedLlmId || null;
        this.hideLlmName = hideLlmName || false;
        this.allowedLlms = (allowedLlms && Array.isArray(allowedLlms) && allowedLlms.length > 0) ? allowedLlms : null;

        if (this.forcedLlmId) {
            // Forced mode: disable selector entirely
            if (this.modelSelectorContainer) {
                this.modelSelectorContainer.style.pointerEvents = 'none';
                this.modelSelectorContainer.style.opacity = '0.6';
            }
            if (this.dropdownIcon) {
                this.dropdownIcon.style.display = 'none';
            }
            // If hide_llm_name, show "AI" instead of model name
            if (this.hideLlmName && this.chatModel) {
                this.chatModel.textContent = 'AI';
            }
        } else if (this.allowedLlms) {
            // Restricted mode: re-populate dropdown with only allowed models
            this.clearRestrictionStyles();
            this.populateModels(this.allowedLlms);
        } else {
            // Any mode: full access
            this.clearRestrictions();
        }
    }

    clearRestrictions() {
        this.forcedLlmId = null;
        this.hideLlmName = false;
        this.allowedLlms = null;
        this.clearRestrictionStyles();
        this.populateModels();
    }

    clearRestrictionStyles() {
        if (this.modelSelectorContainer) {
            this.modelSelectorContainer.style.pointerEvents = '';
            this.modelSelectorContainer.style.opacity = '';
        }
        if (this.dropdownIcon) {
            this.dropdownIcon.style.display = '';
        }
    }
}

// Extension Level Selector functionality
class ExtensionSelector {
    constructor() {
        this.container = document.getElementById('extension-selector-container');
        this.currentName = document.getElementById('extension-current-name');
        this.dropdownMenu = document.getElementById('extension-dropdown-menu');
        this.dropdownContent = document.getElementById('extension-dropdown-content');
        this.dropdownIcon = document.getElementById('extension-dropdown-icon');
        this.extensions = [];
        this.currentExtensionId = null;
        this.freeSelection = true;
        this.isOpen = false;
        this.requestController = null;
        this.requestGeneration = 0;

        if (this.dropdownIcon) {
            this.dropdownIcon.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleDropdown();
            });
            if (this.currentName) {
                this.currentName.addEventListener('click', (e) => {
                    e.stopPropagation();
                    this.toggleDropdown();
                });
            }
        }
        document.addEventListener('click', () => this.closeDropdown());
    }

    init(extensions, activeExtension, freeSelection) {
        this.extensions = extensions || [];
        this.currentExtensionId = activeExtension ? activeExtension.id : null;
        this.freeSelection = freeSelection !== false;

        if (!this.extensions.length) {
            this.hide();
            return;
        }

        this.renderDropdown();
        this.updateCurrentDisplay();
        this.show();
    }

    show() {
        if (this.container) this.container.style.display = '';
    }

    hide() {
        if (this.container) this.container.style.display = 'none';
        this.extensions = [];
        this.currentExtensionId = null;
    }

    toggleDropdown() {
        if (this.isOpen) {
            this.closeDropdown();
        } else {
            this.openDropdown();
        }
    }

    openDropdown() {
        if (this.dropdownMenu) {
            this.dropdownMenu.classList.add('show');
            this.isOpen = true;
        }
    }

    closeDropdown() {
        if (this.dropdownMenu) {
            this.dropdownMenu.classList.remove('show');
            this.isOpen = false;
        }
    }

    renderDropdown() {
        if (!this.dropdownContent) return;
        this.dropdownContent.innerHTML = '';
        this.extensions.forEach(ext => {
            const isActive = ext.id === this.currentExtensionId;
            const isDisabled = !this.freeSelection && !isActive && !this._isAdjacentLevel(ext.id);
            const item = document.createElement('div');
            item.className = 'extension-dropdown-item';
            item.dataset.extensionId = String(ext.id);
            if (isActive) item.classList.add('active');
            if (isDisabled) item.classList.add('disabled');

            const name = document.createElement('span');
            name.className = 'extension-item-name';
            name.textContent = String(ext.name || '');
            item.appendChild(name);

            if (ext.description) {
                const description = document.createElement('span');
                description.className = 'extension-item-desc';
                description.textContent = String(ext.description);
                item.appendChild(description);
            }

            if (!isDisabled) {
                item.addEventListener('click', () => this.selectExtension(ext.id));
            }
            this.dropdownContent.appendChild(item);
        });
    }

    _isAdjacentLevel(extId) {
        if (!this.currentExtensionId) return true;
        const currentIdx = this.extensions.findIndex(e => e.id === this.currentExtensionId);
        const targetIdx = this.extensions.findIndex(e => e.id === extId);
        return Math.abs(currentIdx - targetIdx) <= 1;
    }

    updateCurrentDisplay() {
        if (!this.currentName) return;
        const current = this.extensions.find(e => e.id === this.currentExtensionId);
        this.currentName.textContent = current ? current.name : 'No level';
    }

    cancelPendingRequest() {
        this.requestGeneration += 1;
        if (this.requestController) {
            this.requestController.abort();
            this.requestController = null;
        }
    }

    isRequestCurrent(state) {
        return this.requestGeneration === state.requestGeneration &&
            this.requestController === state.controller &&
            !state.controller.signal.aborted &&
            isCurrentConversationView(state.conversationId, state.viewGeneration);
    }

    async selectExtension(extensionId) {
        if (extensionId === this.currentExtensionId) {
            this.closeDropdown();
            return;
        }
        if (!currentConversationId) return;
        if (this.requestController) {
            this.requestController.abort();
        }
        const state = {
            conversationId: currentConversationId,
            viewGeneration: conversationViewGeneration,
            requestGeneration: ++this.requestGeneration,
            controller: new AbortController()
        };
        this.requestController = state.controller;

        try {
            const resp = await fetch(`/api/conversations/${state.conversationId}/extension`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                signal: state.controller.signal,
                body: JSON.stringify({ extension_id: extensionId })
            });
            if (!this.isRequestCurrent(state)) return;
            if (!resp.ok) {
                const err = await resp.json();
                if (!this.isRequestCurrent(state)) return;
                console.error('Extension switch failed:', err.detail);
                return;
            }
            await resp.json().catch(() => null);
            if (!this.isRequestCurrent(state)) return;
            this.currentExtensionId = extensionId;
            this.renderDropdown();
            this.updateCurrentDisplay();
            this.closeDropdown();
        } catch (e) {
            if (e.name === 'AbortError' || !this.isRequestCurrent(state)) return;
            console.error('Extension switch error:', e);
        } finally {
            if (this.requestController === state.controller) {
                this.requestController = null;
            }
        }
    }

    updateFromSSE(data, sourceConversationId = currentConversationId) {
        if (data && data.id && conversationIdsMatch(currentConversationId, sourceConversationId)) {
            this.currentExtensionId = data.id;
            this.renderDropdown();
            this.updateCurrentDisplay();
        }
    }
}

// Multi-AI Compare Manager
class MultiAiManager {
    constructor() {
        this.enabled = false;
        this.selectedModels = [];  // array of {llm_id, machine, model}
        this.keepActive = false;
        this.maxModels = 4;
        this.modal = null;
        this.init();
    }

    init() {
        const modalEl = document.getElementById('multiAiModal');
        if (modalEl) {
            this.modal = new bootstrap.Modal(modalEl);
        }

        document.getElementById('plus-multi-ai')?.addEventListener('click', () => {
            closePlusMenu();
            this.openModal();
        });

        document.getElementById('multi-ai-apply-btn')?.addEventListener('click', () => {
            this.apply();
        });

        document.getElementById('multi-ai-disable-btn')?.addEventListener('click', () => {
            this.disable();
        });

        document.getElementById('multi-ai-keep-active-check')?.addEventListener('change', (e) => {
            this.keepActive = e.target.checked;
        });
    }

    openModal() {
        this.populateModels();
        this.modal?.show();
    }

    populateModels() {
        const container = document.getElementById('multi-ai-model-list');
        if (!container || !window.availableModels) return;

        const forcedLlmId = window.modelSelector?.forcedLlmId;
        const allowedLlms = window.modelSelector?.allowedLlms;

        // Should not be reachable if visibility is updated correctly, but guard anyway
        if (forcedLlmId) return;

        let models = window.availableModels;
        if (allowedLlms) {
            models = models.filter(m => allowedLlms.includes(m.id));
        }

        // Group by machine (provider)
        const grouped = {};
        models.forEach(m => {
            if (!grouped[m.machine]) grouped[m.machine] = [];
            grouped[m.machine].push(m);
        });

        let html = '';
        const sortedMachines = Object.keys(grouped).sort((a, b) => {
            if (a === 'GPT') return -1;
            if (b === 'GPT') return 1;
            return a.localeCompare(b);
        });

        sortedMachines.forEach(machine => {
            const safeMachine = escapeHtml(machine);
            html += `<div class="multi-ai-provider-group">`;
            html += `<div class="multi-ai-provider-header">${safeMachine}</div>`;
            grouped[machine].sort((a, b) => (a.display_name || a.model).localeCompare(b.display_name || b.model)).forEach(model => {
                const checked = this.selectedModels.some(s => s.llm_id === model.id) ? 'checked' : '';
                const displayText = escapeHtml(String(model.display_name || model.model || ''));
                const safeModelName = escapeHtml(String(model.model || ''));
                const safeMachineName = escapeHtml(String(model.machine || ''));
                html += `
                    <label class="multi-ai-model-item">
                        <input type="checkbox" class="multi-ai-checkbox"
                               data-llm-id="${model.id}"
                               data-machine="${safeMachineName}"
                               data-model="${safeModelName}"
                               ${checked}>
                        <span class="multi-ai-model-name">${displayText}</span>
                    </label>
                `;
            });
            html += `</div>`;
        });

        container.innerHTML = html;

        container.querySelectorAll('.multi-ai-checkbox').forEach(cb => {
            cb.addEventListener('change', (e) => this.onCheckboxChange(e));
        });

        this.updateCount();
    }

    onCheckboxChange(event) {
        const checked = document.querySelectorAll('.multi-ai-checkbox:checked');
        if (checked.length > this.maxModels) {
            event.target.checked = false;
            return;
        }
        this.updateCount();
    }

    updateCount() {
        const checked = document.querySelectorAll('.multi-ai-checkbox:checked');
        const countEl = document.getElementById('multi-ai-count');
        const applyBtn = document.getElementById('multi-ai-apply-btn');

        if (countEl) countEl.textContent = checked.length;
        if (applyBtn) applyBtn.disabled = checked.length < 2;
    }

    apply() {
        const checked = document.querySelectorAll('.multi-ai-checkbox:checked');
        this.selectedModels = Array.from(checked).map(cb => ({
            llm_id: parseInt(cb.dataset.llmId, 10),
            machine: cb.dataset.machine,
            model: cb.dataset.model
        }));

        this.enabled = this.selectedModels.length >= 2;
        this.keepActive = document.getElementById('multi-ai-keep-active-check')?.checked || false;

        this.updateUI();
        this.modal?.hide();
    }

    disable() {
        this.enabled = false;
        this.selectedModels = [];
        this.keepActive = false;

        document.querySelectorAll('.multi-ai-checkbox').forEach(cb => cb.checked = false);
        const keepCheck = document.getElementById('multi-ai-keep-active-check');
        if (keepCheck) keepCheck.checked = false;

        this.updateUI();
        this.modal?.hide();
    }

    updateUI() {
        const badge = document.getElementById('multi-ai-badge');

        if (this.enabled) {
            if (badge) {
                badge.textContent = `${this.selectedModels.length} AIs`;
                badge.classList.add('active');
            }
        } else {
            if (badge) {
                badge.textContent = 'Off';
                badge.classList.remove('active');
            }
        }

        // Integrate with existing plus menu indicator dot
        updatePlusMenuIndicator();
    }

    onConversationChange() {
        if (!this.keepActive) {
            this.disable();
            return;
        }

        const forcedLlmId = window.modelSelector?.forcedLlmId;
        if (forcedLlmId) {
            this.disable();
            return;
        }

        const allowedLlms = window.modelSelector?.allowedLlms;
        if (allowedLlms) {
            this.selectedModels = this.selectedModels.filter(
                m => allowedLlms.includes(m.llm_id)
            );
        }

        if (this.selectedModels.length < 2) {
            this.disable();
            return;
        }

        this.updateUI();
    }

    updateVisibility() {
        const section = document.getElementById('plus-multi-ai-section');
        if (!section) return;

        const forcedLlmId = window.modelSelector?.forcedLlmId;
        if (forcedLlmId) {
            section.style.display = 'none';
            return;
        }

        // Hide Multi-AI when prompt forces web search (Multi-AI disables all tools)
        if (webSearchForced) {
            section.style.display = 'none';
            return;
        }

        const allowedLlms = window.modelSelector?.allowedLlms;
        if (allowedLlms && allowedLlms.length < 2) {
            section.style.display = 'none';
            return;
        }

        const availableCount = allowedLlms
            ? window.availableModels?.filter(m => allowedLlms.includes(m.id)).length || 0
            : window.availableModels?.length || 0;

        section.style.display = availableCount >= 2 ? '' : 'none';
    }

    getModelIds() {
        return this.selectedModels.map(m => m.llm_id);
    }

    afterMessageSent() {
        if (!this.keepActive) {
            this.disable();
        }
    }
}

// Initialize model selector, extension selector, and multi-ai manager when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    window.modelSelector = new ModelSelector();
    window.extensionSelector = new ExtensionSelector();
    window.multiAiManager = new MultiAiManager();
    refreshMemoryHealthBanner();
});

// Platform Mode Management Functions (generalized for WhatsApp and Telegram)
async function loadCurrentPlatformMode(conversationId, platform, voiceModeLink, textModeLink) {
    try {
        const response = await secureFetch(`/api/platform-mode/${platform}/${conversationId}`, {
            method: 'GET',
            headers: {
                'Content-Type': 'application/json',
            }
        });

        if (response && response.ok) {
            const data = await response.json();
            const currentMode = data.mode || 'text';
            updateModeCheckmarks(voiceModeLink, textModeLink, currentMode);
            return true;
        }
        return false;
    } catch (error) {
        console.error(`Error loading ${platform} mode:`, error);
        updateModeCheckmarks(voiceModeLink, textModeLink, 'text');
        return false;
    }
}

function updateModeCheckmarks(voiceModeLink, textModeLink, currentMode) {
    const voiceCheck = voiceModeLink.querySelector('.mode-check');
    const textCheck = textModeLink.querySelector('.mode-check');

    if (currentMode === 'voice') {
        if (voiceCheck) voiceCheck.style.display = 'inline';
        if (textCheck) textCheck.style.display = 'none';
    } else {
        if (voiceCheck) voiceCheck.style.display = 'none';
        if (textCheck) textCheck.style.display = 'inline';
    }
}

const changePlatformMode = withSession(async function(conversationId, platform, newMode) {
    const modeText = newMode === 'voice' ? 'Voice Mode' : 'Text Mode';

    NotificationModal.confirm(
        'Confirm Mode Change',
        `Are you sure you want to switch to ${modeText}?`,
        async function() {
            try {
                const response = await secureFetch(`/api/platform-mode/${platform}/${conversationId}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ mode: newMode })
                });

                if (response && response.ok) {
                    NotificationModal.success('Mode Changed', `The mode has been changed to ${modeText} successfully.`);
                    updatePlatformModeInAllMenus(conversationId, newMode);
                } else {
                    const errorData = await response.json();
                    throw new Error(errorData.error || 'Error changing mode');
                }
            } catch (error) {
                console.error(`Error changing ${platform} mode:`, error);
                NotificationModal.error('Error', `Could not change mode: ${error.message}`);
            }
        }
    );
});

function updatePlatformModeInAllMenus(conversationId, newMode) {
    const chatElements = document.querySelectorAll(`[data-conversation-id="${conversationId}"]`);

    chatElements.forEach(chatElement => {
        const container = chatElement.querySelector('.platform-menu-container');
        if (container) {
            const allModeOptions = container.querySelectorAll('.platform-mode-option');
            let voiceModeLink = null;
            let textModeLink = null;

            allModeOptions.forEach(option => {
                const icon = option.querySelector('i');
                if (icon && icon.classList.contains('fa-microphone')) {
                    voiceModeLink = option;
                } else if (icon && icon.classList.contains('fa-keyboard')) {
                    textModeLink = option;
                }
            });

            if (voiceModeLink && textModeLink) {
                updateModeCheckmarks(voiceModeLink, textModeLink, newMode);
            }
        }
    });
}

function showPhoneRequiredModal(platform) {
    const platformName = platform.charAt(0).toUpperCase() + platform.slice(1);

    const existing = document.getElementById('phoneRequiredModal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'phoneRequiredModal';
    modal.className = 'modal-overlay';
    modal.innerHTML = `
        <div class="modal-content">
            <div class="modal-header">
                <h5>Phone Number Required</h5>
                <button class="modal-close" onclick="this.closest('.modal-overlay').remove()">&times;</button>
            </div>
            <div class="modal-body">
                <p>To use ${platformName}, you need to set your phone number in your profile settings first.</p>
            </div>
            <div class="modal-footer">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                <a href="/settings" class="btn btn-primary">Go to Settings</a>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

// =============================================
// Plus Menu Dropdown
// =============================================

function initPlusMenu() {
    const btn = document.getElementById('plus-menu-btn');
    const dropdown = document.getElementById('plus-menu-dropdown');
    const wrapper = document.getElementById('plus-menu-wrapper');
    if (!btn || !dropdown) return;

    // Toggle dropdown
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (btn.disabled) return;
        dropdown.classList.contains('show') ? closePlusMenu() : openPlusMenu();
    });

    // Close on outside click
    document.addEventListener('click', (e) => {
        if (wrapper && !wrapper.contains(e.target)) {
            closePlusMenu();
        }
    });

    // Attach files
    const attachBtn = document.getElementById('plus-attach-files');
    if (attachBtn) {
        attachBtn.addEventListener('click', () => {
            closePlusMenu();
            document.getElementById('chat-files').click();
        });
    }

    // Record audio (delegates to hidden #audio-button for audio.js compatibility)
    const recordBtn = document.getElementById('plus-record-audio');
    if (recordBtn) {
        recordBtn.addEventListener('click', () => {
            closePlusMenu();
            const audioBtn = document.getElementById('audio-button');
            if (audioBtn) audioBtn.click();
        });
    }

    // Voice call - close dropdown (voice-call.js handles the rest via #plus-voice-call)
    const voiceBtn = document.getElementById('plus-voice-call');
    if (voiceBtn) {
        voiceBtn.addEventListener('click', () => {
            closePlusMenu();
        });
    }
}

function openPlusMenu() {
    const btn = document.getElementById('plus-menu-btn');
    const dropdown = document.getElementById('plus-menu-dropdown');
    if (btn) btn.classList.add('open');
    if (dropdown) dropdown.classList.add('show');
    // Refresh Multi-AI button visibility each time the menu opens
    window.multiAiManager?.updateVisibility();
}

function closePlusMenu() {
    const btn = document.getElementById('plus-menu-btn');
    const dropdown = document.getElementById('plus-menu-dropdown');
    if (btn) btn.classList.remove('open');
    if (dropdown) dropdown.classList.remove('show');
}

// Hide AI features section if all items inside are hidden
function updateAiSectionVisibility() {
    const section = document.getElementById('plus-ai-section');
    if (!section) return;
    const thinkingItem = document.getElementById('plus-thinking-tokens');
    const webSearchItem = document.getElementById('plus-web-search');
    const allHidden =
        (!thinkingItem || thinkingItem.style.display === 'none') &&
        (!webSearchItem || webSearchItem.style.display === 'none');
    section.classList.toggle('hidden-section', allHidden);
}

// Show indicator dot on + button when features are active
function updatePlusMenuIndicator() {
    const btn = document.getElementById('plus-menu-btn');
    if (!btn) return;
    const hasActive = currentThinkingBudget !== 0 || webSearchEnabled || (window.multiAiManager?.enabled === true);
    btn.classList.toggle('has-active', hasActive);
}

// =============================================
// Thinking Tokens Control (Plus Menu Item)
// =============================================

function initializeThinkingTokensControl() {
    const menuItem = document.getElementById('plus-thinking-tokens');
    const popup = document.getElementById('thinking-tokens-popup');
    if (!menuItem || !popup) return;

    const slider = document.getElementById('thinking-tokens-slider');
    const input = document.getElementById('thinking-tokens-input');
    const display = document.getElementById('thinking-tokens-display');
    const applyBtn = document.getElementById('thinking-tokens-apply');
    const presetBtns = document.querySelectorAll('.preset-btn');
    const badge = document.getElementById('thinking-tokens-badge');
    const ANTHROPIC_THINKING_MIN = 1024;

    function isAdaptiveModel() {
        const model = (document.getElementById('chat-model')?.textContent || '').toLowerCase();
        return model.includes('4-6') || model.includes('4.6')
            || model.includes('4-7') || model.includes('4.7')
            || model.includes('4-8') || model.includes('4.8');
    }

    function updateThinkingTokensVisibility() {
        const currentModel = document.getElementById('chat-model')?.textContent || '';
        const modelLower = currentModel.toLowerCase();
        const isSupported =
            modelLower.includes('claude-sonnet-4') ||
            modelLower.includes('claude-opus-4') ||
            modelLower.includes('claude-3.7') ||
            modelLower.includes('claude-3-7') ||
            modelLower.includes('claude-4') ||
            (modelLower.includes('claude') && modelLower.includes('sonnet') && modelLower.includes('4'));

        menuItem.style.display = isSupported ? '' : 'none';
        if (!isSupported) {
            currentThinkingBudget = 0;
        } else {
            // Update first preset button based on adaptive capability
            const firstPreset = presetBtns[0];
            if (firstPreset) {
                if (isAdaptiveModel()) {
                    firstPreset.textContent = 'Auto';
                    firstPreset.dataset.value = '-1';
                    // If was Off (0), auto-set to Auto (-1) for adaptive models
                    if (currentThinkingBudget === 0) {
                        currentThinkingBudget = -1;
                        slider.disabled = true;
                        input.disabled = true;
                    }
                } else {
                    firstPreset.textContent = 'Off';
                    firstPreset.dataset.value = '0';
                    // If was Auto (-1), reset to Off (0) for non-adaptive models
                    if (currentThinkingBudget === -1) {
                        currentThinkingBudget = 0;
                        slider.disabled = false;
                        input.disabled = false;
                    }
                }
            }
            // Opus 4.7+ rejects manual thinking budget: lock UI to Off / Auto only
            const isOpusAdaptiveOnly = modelLower.includes('opus-4-7') || modelLower.includes('opus-4.7')
                || modelLower.includes('opus-4-8') || modelLower.includes('opus-4.8');
            if (isOpusAdaptiveOnly) {
                slider.disabled = true;
                input.disabled = true;
                presetBtns.forEach(btn => {
                    const v = parseInt(btn.dataset.value);
                    btn.disabled = v > 0;
                    btn.classList.toggle('disabled', v > 0);
                });
                if (currentThinkingBudget > 0) {
                    currentThinkingBudget = -1;
                }
            } else {
                presetBtns.forEach(btn => {
                    btn.disabled = false;
                    btn.classList.remove('disabled');
                });
            }
            updateDisplay(currentThinkingBudget);
        }
        updateAiSectionVisibility();
        updatePlusMenuIndicator();
    }

    // Open popup from dropdown item
    menuItem.addEventListener('click', (e) => {
        e.stopPropagation();
        closePlusMenu();
        if (popup.style.display === 'none' || window.getComputedStyle(popup).display === 'none') {
            popup.style.display = 'block';
        } else {
            popup.style.display = 'none';
        }
    });

    // Close popup when clicking outside
    document.addEventListener('click', (e) => {
        const control = document.getElementById('thinking-tokens-control');
        if (control && !control.contains(e.target) && !menuItem.contains(e.target)) {
            popup.style.display = 'none';
        }
    });

    function updateDisplay(value) {
        value = parseInt(value);
        const label = value === -1 ? 'Auto' : value === 0 ? 'Off' : value.toLocaleString();
        if (display) display.textContent = label;
        if (badge) {
            badge.textContent = label;
            badge.classList.toggle('active', value !== 0);
        }
        presetBtns.forEach(btn => {
            btn.classList.toggle('active', parseInt(btn.dataset.value) === value);
        });
        updatePlusMenuIndicator();
    }

    slider.addEventListener('input', (e) => {
        input.value = e.target.value;
        updateDisplay(e.target.value);
    });

    input.addEventListener('input', (e) => {
        let value = parseInt(e.target.value) || 0;
        value = Math.max(0, Math.min(128000, value));
        e.target.value = value;
        if (value <= 20000) slider.value = value;
        updateDisplay(value);
    });

    presetBtns.forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            if (btn.disabled) return;
            const value = parseInt(btn.dataset.value);
            if (value === -1) {
                currentThinkingBudget = -1;
                slider.disabled = true;
                input.disabled = true;
            } else {
                slider.disabled = false;
                input.disabled = false;
                input.value = value;
                if (value <= 20000) slider.value = value;
            }
            updateDisplay(value);
        });
    });

    applyBtn.addEventListener('click', (e) => {
        e.preventDefault();
        if (currentThinkingBudget !== -1) {
            const parsed = parseInt(input.value) || 0;
            currentThinkingBudget = (parsed > 0 && parsed < ANTHROPIC_THINKING_MIN)
                ? ANTHROPIC_THINKING_MIN
                : parsed;
            input.value = currentThinkingBudget;
            slider.value = Math.min(currentThinkingBudget, parseInt(slider.max) || currentThinkingBudget);
        }
        popup.style.display = 'none';
        updateDisplay(currentThinkingBudget);
        const originalText = applyBtn.textContent;
        applyBtn.textContent = 'Applied!';
        setTimeout(() => { applyBtn.textContent = originalText; }, 1000);
    });

    const modelDropdown = document.getElementById('model-dropdown-content');
    if (modelDropdown) {
        modelDropdown.addEventListener('click', () => {
            setTimeout(updateThinkingTokensVisibility, 100);
        });
    }

    window.updateThinkingTokensVisibility = updateThinkingTokensVisibility;
    updateThinkingTokensVisibility();
}

// =============================================
// Web Search Toggle (Plus Menu Item)
// =============================================

function initWebSearchControl(conversationId, webSearchAllowedByPrompt = null, webSearchForcedByPrompt = false) {
    const menuItem = document.getElementById('plus-web-search');
    if (!menuItem) return;

    if (!conversationId) {
        menuItem.style.display = 'none';
        updateAiSectionVisibility();
        updatePlusMenuIndicator();
        return;
    }

    webSearchAllowed = webSearchAllowedByPrompt !== null ? webSearchAllowedByPrompt : true;
    webSearchForced = webSearchForcedByPrompt;
    webSearchEnabled = typeof webSearchUserEnabled !== 'undefined' ? webSearchUserEnabled : true;

    if (webSearchForced) {
        // Prompt forces web search ON - show toggle as active but disabled
        menuItem.style.display = '';
        webSearchEnabled = true;
        updateWebSearchButtonState();
        menuItem.classList.add('forced-active');
        menuItem.style.pointerEvents = 'none';
        menuItem.style.opacity = '0.7';
        menuItem.title = 'Web search is always active for this prompt';
    } else if (webSearchAllowed) {
        // Normal: user can toggle
        menuItem.style.display = '';
        menuItem.classList.remove('forced-active');
        menuItem.style.pointerEvents = '';
        menuItem.style.opacity = '';
        menuItem.title = '';
        updateWebSearchButtonState();
    } else {
        // Prompt disables web search - hide toggle
        menuItem.style.display = 'none';
        menuItem.classList.remove('forced-active');
        menuItem.style.pointerEvents = '';
        menuItem.style.opacity = '';
        menuItem.title = '';
    }
    updateAiSectionVisibility();
    updatePlusMenuIndicator();
}

function updateWebSearchButtonState() {
    const toggleSwitch = document.getElementById('web-search-toggle-switch');
    if (toggleSwitch) {
        toggleSwitch.classList.toggle('active', webSearchEnabled);
    }
    updatePlusMenuIndicator();
}

async function toggleWebSearch() {
    if (!webSearchAllowed || webSearchForced) return;
    try {
        const response = await secureFetch('/api/user/web-search-toggle', {
            method: 'POST'
        });
        if (!response.ok) return;
        const data = await response.json();
        webSearchEnabled = data.web_search_enabled;
        webSearchUserEnabled = webSearchEnabled;
        updateWebSearchButtonState();
    } catch (error) {
        console.error('Error toggling web search:', error);
    }
}

function initWebSearchEventListeners() {
    const menuItem = document.getElementById('plus-web-search');
    if (menuItem) {
        menuItem.addEventListener('click', (e) => {
            e.stopPropagation(); // Keep dropdown open for toggle
            toggleWebSearch();
        });
    }
}

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', function() {
    initPlusMenu();
    initIncognitoChatControls();
    initializeThinkingTokensControl();
    initWebSearchEventListeners();
    setTimeout(() => {
        if (window.updateThinkingTokensVisibility) {
            window.updateThinkingTokensVisibility();
        }
    }, 500);
});

// Make loadMessages globally accessible for voice-call.js
window.loadMessages = loadMessages;


// ---------------------------------------------------------------------------
// GranSabio Pipeline UI
// ---------------------------------------------------------------------------

/**
 * Render or update the GranSabio verbose status panel inside a bot message.
 * Creates a collapsible panel on first call, updates content on subsequent calls.
 */
function renderGranSabioStatus(messageEl, statusData) {
    let panel = messageEl.querySelector('.gransabio-panel');
    if (!panel) {
        panel = document.createElement('div');
        panel.className = 'gransabio-panel';
        panel.innerHTML = `
            <div class="gransabio-header" onclick="this.parentElement.classList.toggle('collapsed')">
                <i class="fas fa-brain"></i>
                <span class="gransabio-title">GranSabio Pipeline</span>
                <span class="gransabio-toggle"><i class="fas fa-chevron-down"></i></span>
            </div>
            <div class="gransabio-body"></div>
        `;
        // Insert before any existing content
        messageEl.insertBefore(panel, messageEl.firstChild);
    }

    const body = panel.querySelector('.gransabio-body');
    const phase = statusData.phase || 'unknown';
    const text = statusData.text || '';

    // Build status line
    const line = document.createElement('div');
    line.className = `gransabio-line gransabio-phase-${phase}`;

    let icon = 'fa-cog fa-spin';
    if (phase === 'generating') icon = 'fa-pen-nib';
    else if (phase === 'qa') icon = 'fa-check-double';
    else if (phase === 'scoring') icon = 'fa-calculator';
    else if (phase === 'editing') icon = 'fa-edit';
    else if (phase === 'retry') icon = 'fa-redo';
    else if (phase === 'gran_sabio') icon = 'fa-brain';
    else if (phase === 'arbiter') icon = 'fa-balance-scale';
    else if (phase === 'preflight') icon = 'fa-plane-departure';
    else if (phase === 'status') icon = 'fa-info-circle';

    let extra = '';
    if (statusData.iteration && statusData.max_iterations) {
        extra += ` <span class="gransabio-iter">iter ${statusData.iteration}/${statusData.max_iterations}</span>`;
    }
    if (statusData.layer_name) {
        extra += ` <span class="gransabio-layer">${DOMPurify.sanitize(statusData.layer_name)}</span>`;
    }
    if (statusData.score !== undefined && statusData.min_score !== undefined) {
        const passed = statusData.passed !== false;
        const scoreClass = passed ? 'gransabio-score-pass' : 'gransabio-score-fail';
        extra += ` <span class="${scoreClass}">${statusData.score.toFixed(1)}/${statusData.min_score.toFixed(1)}</span>`;
    }

    line.innerHTML = `<i class="fas ${icon}"></i> <span class="gransabio-text">${DOMPurify.sanitize(text)}</span>${extra}`;
    body.appendChild(line);

    // Keep only last 50 lines to prevent DOM bloat
    while (body.children.length > 50) {
        body.removeChild(body.firstChild);
    }
}

/**
 * Finalize the GranSabio panel: collapse, show summary line.
 */
function finalizeGranSabioStatus(messageEl, summaryData) {
    const panel = messageEl.querySelector('.gransabio-panel');
    if (!panel) return;

    // Collapse the panel
    panel.classList.add('collapsed');

    // Update header with summary
    const title = panel.querySelector('.gransabio-title');
    if (title && summaryData) {
        const approved = summaryData.approved === true;
        const score = summaryData.final_score != null ? summaryData.final_score.toFixed(1) : '?';
        const iters = summaryData.iterations_used || summaryData.iterations || '?';
        const cost = summaryData.total_cost != null ? `$${summaryData.total_cost.toFixed(4)}` :
                     (summaryData.api_cost != null ? `$${summaryData.api_cost.toFixed(4)}` : '');
        const status = approved ? 'Approved' : 'Failed';
        title.textContent = `GranSabio: ${status} (${score}, ${iters} iter${cost ? ', ' + cost : ''})`;
        title.classList.add(approved ? 'gransabio-approved' : 'gransabio-failed');
    }

    // Stop any spinning icons
    panel.querySelectorAll('.fa-spin').forEach(el => el.classList.remove('fa-spin'));
}
