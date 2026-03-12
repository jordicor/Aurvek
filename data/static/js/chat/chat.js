/* chat.js */

let oldestLoadedMessageId = null;
let limit = 25;
let initialLoad = true;
let isLoading = false;
let isLoadingConversations = false;
let allMessagesLoaded = false;
let allConversationsLoaded = false;
let lowestLoadedId = Infinity;
let currentAbortController = null;
let currentTimer = null;
let lastSelectedConversationId = null;
var botname = "bot";
const limitMessage = 25;
const processedMessageIds = new Set();
let currentThinkingBudget = 0;
let isCurrentConversationLocked = false;

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

// =============================================
// API Key Mode Manager
// =============================================
const ApiKeyManager = {
    mode: typeof apiKeyMode !== 'undefined' ? apiKeyMode : 'both_prefer_own',
    canSend: typeof canSendMessages !== 'undefined' ? canSendMessages : true,
    requiresOwn: typeof requiresOwnKeys !== 'undefined' ? requiresOwnKeys : false,
    hasOwn: typeof hasOwnKeys !== 'undefined' ? hasOwnKeys : false,

    /**
     * Check if user can send messages based on API key configuration
     * @returns {boolean}
     */
    canSendMessages: function() {
        return this.canSend;
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

        if (this.canSend) {
            // Hide banner and enable input
            if (banner) banner.style.display = 'none';
            if (inputContainer) inputContainer.removeAttribute('data-disabled');
        } else {
            // Show banner and disable input
            if (banner) banner.style.display = 'block';
            if (inputContainer) inputContainer.setAttribute('data-disabled', 'true');
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
            imgElement.dataset.fullsize = messageObj.url.replace('_256.webp', '_fullsize.webp');
            imgElement.dataset.messageId = messageId;
            imgElement.onclick = function() {
                imageHandler.showFullsize(this.dataset.fullsize, this.dataset.messageId);
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
    
        if (messageObj && messageObj.is_bookmarked) {
            bookmarkIcon.classList.add('bookmarked');
            bookmarkIcon.style.display = 'inline';
        }
    
        const conversationId = messageObj && messageObj.conversation_id ? messageObj.conversation_id : currentConversationId;
        bookmarkIcon.dataset.conversationId = conversationId;
    
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
        copyIcon.onclick = function() {
            copyToClipboard(resolveMessageText(), copyIcon);
        };
    
        iconContainer.appendChild(audioIcon);
        iconContainer.appendChild(bookmarkIcon);
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

        if (author === 'bot') {
            const rollbackIcon = document.createElement('i');
            rollbackIcon.classList.add('fas', 'fa-undo', 'rollback-icon');
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
        this.querySelectorAll('.fa-bookmark, .fa-copy, .fa-code-branch, .fa-undo, .fa-arrow-right').forEach(icon => {
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
        this.querySelectorAll('.fa-bookmark, .fa-copy, .fa-code-branch, .fa-undo, .fa-arrow-right').forEach(icon => {
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
    const codeRegex = /<pre><code class="language-(\w+)">([\s\S]*?)<\/code><\/pre>/g;
    return html.replace(codeRegex, function(match, language, code) {
        // Remove whitespace at start and end of code
        code = code.trim();
        // Remove common indentation
        const lines = code.split('\n');
        const commonIndent = lines.reduce((min, line) => {
            const indent = line.match(/^\s*/)[0].length;
            return line.trim() ? Math.min(min, indent) : min;
        }, Infinity);
        const dedentedCode = lines.map(line => line.slice(commonIndent)).join('\n');
        
        // Create HTML without spaces or line breaks between tags
        return `<div class="code-block"style="display:grid"><div class="code-header"><span class="code-language">${language}</span><button class="copy-button" onclick="copyCode(this)"><i class="fas fa-copy"></i></button></div><pre><code class="language-${language}">${dedentedCode}</code></pre></div>`;
    });
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

function sendMessage(messageText) {
    // Check if user can send messages based on API key configuration
    if (!ApiKeyManager.canSendMessages()) {
        ApiKeyManager.handleApiKeyError({ error: 'api_keys_required', action: 'configure_api_keys' });
        return;
    }

    if (currentConversationId === null) {
        console.error('No conversation selected');
        return;
    }
    if (!messageText.trim()) {
        return;
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

    let userMessageElement = addMessage(
        'user',
        messageText_raw,
        { timestamp: convertToLocalTime(timestamp), isNewMessage: true },
        false,
        { type: 'text', text: messageText_raw }
    );

    // Get the user message ID
    //getLastMessageId(userMessageElement)

    const multiAiLoadingText = isMultiAiRequest
        ? `Comparing ${selectedMultiAiModels.length} AI models...`
        : '';

    addLoadingIndicator(multiAiLoadingText);

    var files = document.getElementById('image-files').files;
    var formData = new FormData();

    // Compress message with pako (maximum compression level)
    const compressedMessage = pako.deflate(messageText_raw, { level: 9 });
    formData.append('text_compressed', new Blob([compressedMessage], { type: 'application/octet-stream' }));
    formData.append('is_compressed', 'true'); // Indicate that message is compressed
    
    // Add thinking budget tokens if set (-1 = auto, > 0 = manual)
    if (currentThinkingBudget !== 0) {
        formData.append('thinking_budget_tokens', currentThinkingBudget);
    }

    // Multi-AI: append model IDs if active
    if (isMultiAiRequest) {
        // Block file attachments in Multi-AI v1
        if (attachedFiles && attachedFiles.length > 0) {
            NotificationModal.warning(
                'Multi-AI',
                'File attachments are not supported in Multi-AI Compare mode. Please disable Multi-AI or remove the attached files.'
            );
            if (userMessageElement) userMessageElement.remove();
            removeLoadingIndicator();
            document.getElementById('message-text').disabled = false;
            document.getElementById('message-text').focus();
            return;
        }
        formData.append('multi_ai_models', JSON.stringify(selectedMultiAiModels.map((model) => model.llm_id)));
    }

    var imagePreviews = document.getElementById('image-previews');
    imagePreviews.innerHTML = '';

    processFiles(attachedFiles, formData, imagePreviews);
    attachedFiles = [];

    controller = new AbortController();
    const signal = controller.signal;

    toggleSendButton('Stop');

    secureFetch(`/api/conversations/${currentConversationId}/messages`, {
        method: 'POST',
        body: formData,
        signal: signal,
    })
    .then(response => {
        const restoreInputAfterFailure = () => {
            if (userMessageElement) userMessageElement.remove();
            removeLoadingIndicator();
            toggleSendButton('Send');
            document.getElementById('message-text').disabled = false;
            const submitBtn = document.querySelector('#form-message button[type="submit"]');
            if (submitBtn) submitBtn.disabled = false;
            document.getElementById('message-text').focus();
        };

        const extractServerError = (resp) => {
            if (!resp) return Promise.resolve('Request failed');
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
            return null;
        }
        if (response.status === 401) {
            return response.json().then(data => {
                if (data.redirect) {
                    window.location.href = data.redirect;
                    return null;
                }
            });
        }
        if (response.status === 403) {
            restoreInputAfterFailure();
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
            restoreInputAfterFailure();
            return extractServerError(response).then((msg) => {
                NotificationModal.error('Send failed', String(msg));
                return null;
            });
        }

        if (!response.body) {
            restoreInputAfterFailure();
            NotificationModal.error('Send failed', 'No response stream received from server.');
            return null;
        }

        return response.body.getReader();
    })
    .then(reader => {
        if (!reader) return; // If there was redirection, reader will be undefined
        
        const sseDecoder = new TextDecoder('utf-8');
        let sseBuffer = '';
        let botMessageText = '';
        let endConversation = false;
        let newMessageId = null;
        let newUserMessageId = null;
        let updatedChatName = null;
        let streamCitations = null;

        // Create the bot message element before starting to read the stream
        const botMessageElement = document.createElement('div');
        botMessageElement.classList.add('message', 'text-white', 'bot');

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

        let botMessageParagraph = null;
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

        const audioIcon = createIcon('fa-volume-up', 'inline', () => textToSpeech(getCurrentBotText(), user_id, currentConversationId, audioIcon, 'bot'));
        audioIcon.dataset.baseIcon = 'fa-volume-up';
        const bookmarkIcon = createIcon('fa-bookmark', 'none', function() {
            const messageElement = this.closest('.message');
            const messageId = messageElement ? messageElement.dataset.messageId : null;
            if (messageId) {
                toggleBookmark(messageId, currentConversationId, this);
            } else {
                console.error('Could not find message ID to mark as favorite');
            }
        });
        const copyIcon = createIcon('fa-copy', 'none', () => copyToClipboard(getCurrentBotText(), copyIcon));
        const branchIcon = createIcon('fa-code-branch', 'none', () => branchConversation(newMessageId, currentConversationId));
        const rollbackIcon = createIcon('fa-undo', 'none', () => rollbackConversation(newMessageId, currentConversationId));

        iconContainer.appendChild(audioIcon);
        iconContainer.appendChild(bookmarkIcon);
        iconContainer.appendChild(copyIcon);
        iconContainer.appendChild(branchIcon);
        iconContainer.appendChild(rollbackIcon);

        const timeSpan = document.createElement('span');
        infoContainer.appendChild(iconContainer);
        infoContainer.appendChild(timeSpan);
        messageContent.appendChild(infoContainer);

        function createIcon(iconClass, styleDisplay, onClickFunction, messageId = null) {
            const icon = document.createElement('i');
            icon.classList.add('fas', iconClass);
            icon.style.cursor = 'pointer';
            icon.style.display = styleDisplay;
            if (messageId && (iconClass === 'fa-undo' || iconClass === 'fa-code-branch')) {
                icon.setAttribute('data-message-id', messageId);
                if (iconClass === 'fa-undo') {
                    icon.onclick = function() {
                        rollbackConversation(this.getAttribute('data-message-id'), currentConversationId);
                    };
                } else {
                    icon.onclick = function() {
                        branchConversation(this.getAttribute('data-message-id'), currentConversationId);
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
            return reader.read().then(({ done, value }) => {
				if (done) {
					toggleSendButton('Send');
					initializeNewImages(botMessageElement);
					if (updatedChatName) {
						updateActiveChatName(updatedChatName);
					}
					return;
				}				
                sseBuffer += sseDecoder.decode(value, { stream: true });
                const lines = sseBuffer.split('\n');
                // Keep the last (potentially incomplete) line in the buffer
                sseBuffer = lines.pop();
        
                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const data = line.slice(6).trim();
						
                        if (data === '[DONE]') {
                            toggleSendButton('Send');
                            initializeNewImages(botMessageElement);
                            return;
                        }
                        try {
                            const parsedData = JSON.parse(data);

                            // Handle actions from AI welfare system
                            if (parsedData.action === 'end_conversation') {
                                endConversation = true;
                                isCurrentConversationLocked = true;
                                // Update UI to show locked state
                                const selectedChat = document.querySelector(`.list-group-item[data-conversation-id="${currentConversationId}"]`);
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
                                const lockedBanner = document.getElementById('locked-conversation-banner');
                                if (lockedBanner) lockedBanner.style.display = 'flex';
                            }
                            // Note: pass_turn action sends '🚩' as content, which displays
                            // as a normal bot message. No special handling needed.

                            // Handle thinking tokens streaming
                            if (parsedData.type === 'thinking_start') {
                                if (messageContent && botMessageParagraph) {
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
                                multiAiCarousel._multiAiApi.appendChunk(parsedData.llm_id, parsedData.content || '');
                                botMessageText = getActiveMultiAiSlideText(botMessageElement);
                                scrollToBottomIfNeeded();
                            } else if (parsedData.multi_ai_done && multiAiCarousel?._multiAiApi) {
                                multiAiCarousel._multiAiApi.markSlideDone(parsedData.llm_id);
                            } else if (parsedData.multi_ai_error && multiAiCarousel?._multiAiApi) {
                                multiAiCarousel._multiAiApi.setSlideError(parsedData.llm_id, parsedData.error || 'Unknown error');
                                scrollToBottomIfNeeded();
                            } else if (parsedData.error && !parsedData.multi_ai_error) {
                                console.error('SSE error:', parsedData.error);
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
                            } else if (parsedData.content && !parsedData.multi_ai && botMessageParagraph) {
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
                                window.extensionSelector.updateFromSSE(parsedData.extension_changed);
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
            document.querySelector('#form-message button[type="submit"]').disabled = false;
            document.getElementById('message-text').focus();
            scrollToBottomIfNeeded();

            isCurrentConversationEmpty = false;
            attachedFiles = [];
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

            // Add event listeners to show/hide icons
            botMessageElement.addEventListener('mouseover', function() {
                const messageId = this.dataset.messageId;
                this.querySelectorAll('.fa-volume-up, .fa-bookmark, .fa-copy, .fa-code-branch, .fa-undo').forEach(icon => {
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
                this.querySelectorAll('.fa-volume-up, .fa-bookmark, .fa-copy, .fa-code-branch, .fa-undo').forEach(icon => {
                    if (!icon.classList.contains('bookmarked')) {
                        icon.style.display = 'none';
                    }
                });
            });
        });
    })
    .catch(error => {
        console.error('Error:', error);
        removeLoadingIndicator();
        toggleSendButton('Send');
        document.getElementById('message-text').disabled = false;
        document.querySelector('#form-message button[type="submit"]').disabled = false;
        document.getElementById('message-text').focus();

        // Reset text field to original size in case of error
        document.getElementById('message-text').style.height = 'auto';
    });
}

function updateMessageId(messageId, element) {
    if (element) {
        const isBotMessage = element.classList.contains('bot');
        element.dataset.messageId = messageId;
        const rollbackIcon = element.querySelector('.fa-undo');
        if (rollbackIcon) {
            rollbackIcon.setAttribute('data-message-id', messageId);
            rollbackIcon.style.display = 'inline';
            rollbackIcon.onclick = function() {
                rollbackConversation(messageId, currentConversationId);
            };
        } else if (isBotMessage) {
        }
        const branchIcon = element.querySelector('.fa-code-branch');
        if (branchIcon) {
            branchIcon.setAttribute('data-message-id', messageId);
            branchIcon.onclick = function() {
                branchConversation(messageId, currentConversationId);
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
    conversationElement.dataset.locked = conversation.locked ? 'true' : 'false';
    conversationElement.dataset.webSearchAllowed = conversation.web_search_allowed !== false ? 'true' : 'false';
    conversationElement.dataset.webSearchForced = conversation.web_search_forced === true ? 'true' : 'false';
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
    if (conversation.locked) {
        conversationElement.classList.add('conversation-locked');
    }
    if (conversation.external_platform) {
        conversationElement.dataset.externalPlatform = conversation.external_platform;
    }
    if (conversation.id === currentConversationId) {
        conversationElement.classList.add('active-chat');
    }

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

    // Close menu when clicking outside
    document.addEventListener('click', (e) => {
        if (!chatMenu.contains(e.target) && !chatMenuContent.contains(e.target) && chatMenuContent.style.display === 'block') {
            closeAllChatMenus();
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
        // Unassigned — check if conversation is already assigned to another platform
        const otherPlatform = conversation.external_platform;
        if (otherPlatform && otherPlatform !== platform) {
            const link = document.createElement('a');
            link.href = '#';
            link.classList.add('platform-link', 'disabled');
            link.innerHTML = `<i class="fab ${icon}"></i> Use for ${platformName}`;
            link.title = `Already assigned to ${otherPlatform}`;
            link.style.opacity = '0.5';
            link.style.pointerEvents = 'none';
            return link;
        }

        const link = document.createElement('a');
        link.href = '#';
        link.classList.add('platform-link');
        link.innerHTML = `<i class="fab ${icon}"></i> Use for ${platformName}`;
        link.addEventListener('click', function(e) {
            e.stopPropagation();
            closeAllChatMenus();
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
            updateConversationElement(conversationId, data.updatedConversations.find(conv => conv.id === parseInt(conversationId)), data.updatedConversations);
        } else if (data.error === 'no_phone_number') {
            showPhoneRequiredModal(platform);
        } else {
            console.error('Error updating external platform:', data.error);
        }
    })
    .catch(error => {
        console.error('Error:', error);
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
    sortDynamicChats(dynamicChatsContainer);

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

function sortDynamicChats(dynamicChatsContainer) {
    const chats = Array.from(dynamicChatsContainer.children);
    chats.sort((a, b) => {
        const idA = parseInt(a.dataset.conversationId);
        const idB = parseInt(b.dataset.conversationId);
        return idB - idA; // Descending order, change to idA - idB for ascending order
    });
    
    chats.forEach(chat => dynamicChatsContainer.appendChild(chat));
}

function updateSingleConversation(element, conversationData, externalContainer, dynamicContainer) {
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
    element.dataset.machine = conversationData.machine || 'undefined';

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
    element.innerHTML = '';
    element.appendChild(nameSpan);

    const chatMenu = createChatMenu(conversationData);
    element.appendChild(chatMenu);

    if (element.parentElement !== targetContainer) {
        if (targetContainer === externalContainer) {
            const currentExternalChat = externalContainer.firstElementChild;
            if (currentExternalChat) {
                currentExternalChat.classList.remove('active-chat');
                dynamicContainer.insertBefore(currentExternalChat, dynamicContainer.firstChild);
            }
            externalContainer.innerHTML = '';
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
    if (!e.target.closest('.chat-menu')) {
        var conversationId = this.getAttribute('data-conversation-id');
        var chatNameElement = this.querySelector('.chat-name');
        var chatName = chatNameElement ? chatNameElement.textContent.trim() : `Chat ${conversationId}`;
        var machine = this.getAttribute('data-machine');
        
        if (conversationId) {
            
            // Remove active-chat class from ALL chats everywhere
            document.querySelectorAll('.active-chat').forEach(el => {
                el.classList.remove('active-chat');
            });
            
            // Add active-chat class to this element
            this.classList.add('active-chat');
            
            // Update global selectedChat variable
            window.selectedChat = this;
            
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
    currentConversationId = null;
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
    if (lowestLoadedId !== Infinity) {
        url += `&max_id=${lowestLoadedId - 1}`;
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
        // Exclude external platform conversations (WhatsApp, Telegram, etc.) from
        // pagination cursor — their low IDs would skip normal conversations
        if (!conversation.external_platform) {
            lowestLoadedId = Math.min(lowestLoadedId, conversation.id);
        }
    });

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

function continueConversation(conversationId, chatName, machine, isInit = false, targetMessageId = null, conversationData = null) {
    removeOverlay();

    // Check if this conversation is already loaded (compare with current conversation ID)
    if (currentConversationId &&
        currentConversationId.toString() === conversationId.toString() &&
        !isInit && !targetMessageId) {
        return Promise.resolve();
    }

    // Same conversation but jumping to a specific message: reset state and reload
    if (currentConversationId &&
        currentConversationId.toString() === conversationId.toString() &&
        targetMessageId) {
        oldestLoadedMessageId = null;
        allMessagesLoaded = false;
        if (currentAbortController) {
            currentAbortController.abort();
            currentAbortController = null;
        }
        isLoading = false;
        processedMessageIds.clear();
        document.getElementById('chat-messages-container').innerHTML = '';
        return loadMessages(conversationId, false, targetMessageId, 0);
    }

    const selectedChat = document.querySelector(`[data-conversation-id="${conversationId}"]`);   

    hideScrollNavButtons();
    oldestLoadedMessageId = null;
    allMessagesLoaded = false;
    // Abort any in-flight request before resetting the loading flag
    // to prevent the old request's finally block from racing with us
    if (currentAbortController) {
        currentAbortController.abort();
        currentAbortController = null;
    }
    isLoading = false;
    processedMessageIds.clear();
    localStorage.setItem('activeConversationId', conversationId);
    currentConversationId = conversationId;
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
        loadMessages(conversationId, false, targetMessageId).then(() => {
            setupInfiniteScroll(conversationId);

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
            updateChatHeader(conversationId, chatTitleText, llmModel);

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

            // Balance-based input visibility (runs after locked check)
            if (!isCurrentConversationLocked) {
                const isPaid = conversationData != null
                    ? !!conversationData.is_paid
                    : (selectedChat?.dataset?.isPaid === '1');
                const hasByok = typeof hasOwnKeys !== 'undefined' && hasOwnKeys && apiKeyMode !== 'system_only';
                const userBal = parseFloat(document.getElementById('user-balance')?.getAttribute('data-balance') || '0');
                const formMessageEl = document.getElementById('form-message');
                const insufficientEl = document.getElementById('insufficient-balance-message');

                if (userBal === 0 && !hasByok) {
                    // Non-BYOK user with zero balance - always hide input
                    formMessageEl.style.display = 'none';
                    insufficientEl.style.display = 'block';
                } else if (hasByok && isPaid && userBal < 0.10) {
                    // BYOK user on paid prompt with insufficient balance for creator markup
                    formMessageEl.style.display = 'none';
                    insufficientEl.style.display = 'block';
                } else {
                    // Sufficient balance or free prompt - ensure input visible
                    formMessageEl.style.display = 'flex';
                    insufficientEl.style.display = 'none';
                }
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

function updateChatHeader(conversationId, chatName, llmModel = null) {
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
    if (llmModel) {
        chatModel.textContent = (window.modelSelector && window.modelSelector.hideLlmName) ? 'AI' : llmModel;
        if (window.modelSelector) {
            window.modelSelector.updateCurrentModel(llmModel);
        }
    } else {
        // Fallback: fetch from API (for new conversations without cached data)
        fetch(`/api/conversations/${conversationId}/details`)
            .then(response => response.json())
            .then(data => {
                const modelInfo = data.model || 'Unknown Model';
                chatModel.textContent = modelInfo;

                // Update model selector state if it exists
                if (window.modelSelector) {
                    window.modelSelector.updateCurrentModel(modelInfo);
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
                console.error('Error fetching conversation details:', error);
                chatModel.textContent = '';
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

function processMessage(message, container, prepend = false) {
    if (processedMessageIds.has(message.id)) return;
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
            processedMessageIds.add(message.id);
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
                } else if (item.type === 'image' && item.source.type === 'base64') {
                    messageObj = {
                        type: 'image_url',
                        url: item.source.data,
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
    processedMessageIds.add(message.id);
}

async function loadMessages(conversationId, prepend = false, targetMessageId = null, attempt = 0) {
    if (isLoading || allMessagesLoaded) return Promise.resolve();
    if (currentAbortController) {
        currentAbortController.abort();
    }

    currentAbortController = new AbortController();
    const signal = currentAbortController.signal;
    isLoading = true;

    disableInputControls();

    const chatWindow = document.getElementById('chat-window');
    const chatMessagesContainer = document.getElementById('chat-messages-container');

    let didRecurse = false;
    try {
        let url = `/api/conversations/${conversationId}/messages?limit=${limitMessage}`;
        if (oldestLoadedMessageId !== null && prepend) {
            url += `&before_id=${oldestLoadedMessageId}`;
        }
        const response = await secureFetch(url, { signal });
        if (!response) {
            isLoading = false;
            enableInputControls();
            return null;
        }

        if (!response.ok) {
            throw new Error('Network response was not ok');
        }
        const data = await response.json();
        const messages = data.messages;
        const conversationInfo = data.conversation_info;

        allMessagesLoaded = !data.has_more;

        if (!prepend) {
            chatMessagesContainer.innerHTML = '';
        }

        const tempDiv = document.createElement('div');

		// name of the prompt (bot)
        botname = conversationInfo.prompt_name;

		// prompt description (bot)
		promptDescription = conversationInfo.prompt_description

        // Update the bot's profile picture
        botProfilePicture = conversationInfo.bot_profile_picture;

        // Sync is_paid flag to sidebar element so continueConversation() can read it
        const sidebarEl = document.querySelector(`[data-conversation-id="${conversationId}"]`);
        if (sidebarEl) {
            sidebarEl.dataset.isPaid = conversationInfo.is_paid ? '1' : '0';
        }

        // Initialize extension selector from message endpoint data
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

        if (prepend) {
            // Anchor to the first currently visible message to prevent scroll jump
            const anchor = chatMessagesContainer.firstElementChild;
            const anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;

            chatMessagesContainer.insertBefore(tempDiv, chatMessagesContainer.firstChild);

            // Restore: keep the anchor element at the same visual position
            if (anchor) {
                const newAnchorTop = anchor.getBoundingClientRect().top;
                chatWindow.scrollTop += (newAnchorTop - anchorTop);
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
            didRecurse = true;
            isLoading = false;
            return loadMessages(conversationId, true, targetMessageId, attempt + 1);
        } else if (targetMessageId && targetMessageFound) {
            setTimeout(() => {
                highlightAndScrollToMessage(targetMessageId);
            }, 100);
        } else if (!targetMessageId) {
            enableInputControls();
        }
    } catch (error) {
        if (error.name === 'AbortError') {
        } else if (error.message === 'Session expired') {
        } else {
            console.error('Error loading messages:', error);
        }
    } finally {
        if (!didRecurse) {
            isLoading = false;
            enableInputControls();
        }
    }
}

function refreshActiveConversation() {
    if (!currentConversationId) {
        return Promise.resolve();
    }

    // Reset pagination so the next fetch pulls fresh messages.
    oldestLoadedMessageId = null;
    allMessagesLoaded = false;
    isLoading = false;

    if (currentAbortController) {
        currentAbortController.abort();
        currentAbortController = null;
    }

    processedMessageIds.clear();

    return loadMessages(currentConversationId, false);
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
    document.getElementById('image-files').disabled = false;
    const plusBtn = document.getElementById('plus-menu-btn');
    if (plusBtn) plusBtn.disabled = false;
    document.getElementById('loading-indicator').style.display = 'none';
}

function disableInputControls() {
    document.querySelector('#form-message button[type="submit"]').disabled = true;
    document.getElementById('image-files').disabled = true;
    const plusBtn = document.getElementById('plus-menu-btn');
    if (plusBtn) plusBtn.disabled = true;
    closePlusMenu();
    document.getElementById('loading-indicator').style.display = 'block';
}


function setupInfiniteScroll(conversationId) {
    const chatWindow = document.getElementById('chat-window');
    chatWindow.onscroll = function() {
        // Infinite scroll: load older messages when at top
        if (chatWindow.scrollTop <= 1 && !isLoading && !allMessagesLoaded) {
            loadMessages(conversationId, true);
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

function startNewConversation(promptId = null) {
    if (admin_view) {
        return Promise.resolve();
    }

    if (!isFirstCall && isCurrentConversationEmpty) {
        return Promise.resolve();
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

    return secureFetch('/api/conversations/new', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(body)
    })
    .then(response => {
        if (!response) {
            // secureFetch returned null (likely session expired)
            throw new Error('Session expired');
        }
        return response.json();
    })
    .then(data => {
        startDate = new Date(); 
        addConversationElement(data, data.name, null, true);
        return continueConversation(data.id, data.name, data.machine, false, null, data);
    })
    .then(() => {
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
    const correctConversationId = bookmarkIcon.dataset.conversationId || conversationId;

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
        return;
    }

    // Clear dedup set so messages already loaded in the conversation view
    // can be rendered again in the bookmarks view
    processedMessageIds.clear();

    fetch(`/api/bookmarks`)
    .then(response => response.json())
    .then(messages => {
        const chatMessagesContainer = document.getElementById('chat-messages-container');
        chatMessagesContainer.innerHTML = '';
        
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
        
        // Clear global conversation state so the guard in continueConversation()
        // won't block reloading the same conversation after leaving bookmarks
        currentConversationId = null;

        // Group messages by conversation (local variable)
        let localConversationId = null;
        messages.forEach(message => {
            if (message.conversation_id !== localConversationId) {
                localConversationId = message.conversation_id;
                const header = document.createElement('div');
                header.className = 'bookmark-conversation-header';
                header.title = 'Click to go to conversation';
                header.innerHTML = `<span class="bookmark-conversation-name">${message.chat_name || 'Chat ' + message.conversation_id}</span>`;
                header.addEventListener('click', () => {
                    continueConversation(message.conversation_id, message.chat_name);
                });
                chatMessagesContainer.appendChild(header);
            }
            processMessage(message, chatMessagesContainer);
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
    .catch(error => console.error('Error loading bookmarked messages:', error));
}

document.getElementById('my-bookmarks-btn').addEventListener('click', function(e) {
    e.preventDefault();
    loadBookmarkedMessages();
});

document.addEventListener('click', function(e) {
    if (e.target && e.target.classList.contains('list-group-item-action')) {
        var conversationId = e.target.getAttribute('data-conversation-id');
        var chatName = e.target.textContent.trim();
        var machine = e.target.getAttribute('data-machine'); 
        if (conversationId) {
			// Check if there are open websockets before trying to stop the audio
			if (ws && ws.readyState === WebSocket.OPEN) {
				stopAudioAndWebSocket();
			}
            continueConversation(conversationId, chatName, machine);
            
            // Deactivate My Bookmarks
            document.getElementById('my-bookmarks-btn').classList.remove('active-bookmarks');
        }
    } else if (e.target && (e.target.id === 'my-bookmarks-btn' || e.target.closest('#my-bookmarks-btn'))) {
        loadBookmarkedMessages();
    }
});

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

    if (botProfilePicture) {
        const img = document.createElement('img');
        img.src = botProfilePicture.replace('_32', '_128');
        img.style.position = 'absolute';
        img.style.top = '0';
        img.style.left = '0';
        img.style.width = '100%';
        img.style.height = '100%';
        img.style.objectFit = 'cover';

        img.style.cursor = 'pointer';
        img.dataset.fullsize = botProfilePicture.replace('_32', '_fullsize');
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
        pillsDiv.innerHTML = window.extensionSelector.extensions.map(ext => {
            const isActive = ext.id === window.extensionSelector.currentExtensionId;
            return `<span class="extension-pill${isActive ? ' active' : ''}">${ext.name}</span>`;
        }).join('');
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
            const sortedModels = groupedModels[machine].sort((a, b) => a.model.localeCompare(b.model));
            
            sortedModels.forEach(model => {
                // const visionBadge = model.vision ? '<span class="vision-badge">Vision</span>' : '';
                const safeModel = escapeHtml(String(model.model || ''));
                html += `
                    <div class="model-item" data-llm-id="${model.id}" data-model="${safeModel}">
                        <span>${safeModel}</span>
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
    
    updateCurrentModel(modelName) {
        this.currentModel = modelName;
        
        // Find the llm_id for this model
        if (window.availableModels) {
            const modelData = window.availableModels.find(m => m.model === modelName);
            if (modelData) {
                this.currentLlmId = modelData.id;
            }
        }
        
        // Update UI to show current model
        this.updateModelDisplay();
        
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
            if (item.dataset.model === this.currentModel) {
                item.classList.add('current');
            }
        });
    }
    
    async selectModel(llmId, modelName) {
        if (!currentConversationId || llmId === this.currentLlmId) {
            this.closeDropdown();
            return;
        }
        
        try {
            // Show loading state
            this.chatModel.textContent = 'Updating...';
            
            const response = await fetch(`/api/conversations/${currentConversationId}/model`, {
                method: 'PATCH',
                headers: {
                    'Content-Type': 'application/json',
                },
                credentials: 'include',
                body: JSON.stringify({
                    llm_id: llmId
                })
            });
            
            if (!response.ok) {
                const errBody = await response.json().catch(() => ({}));
                throw new Error(errBody.detail || `Failed to update model`);
            }
            
            const result = await response.json();
            
            if (result.success) {
                // Update local state
                this.currentModel = result.model;
                this.currentLlmId = llmId;
                
                // Update UI
                this.chatModel.textContent = result.model;
                this.updateModelDisplay();
                
                // Update thinking tokens visibility for new model
                if (window.updateThinkingTokensVisibility) {
                    window.updateThinkingTokensVisibility();
                }
                
                // Show success feedback
                this.showSuccess();
                
            } else {
                throw new Error('Failed to update model');
            }
            
        } catch (error) {
            console.error('Error updating model:', error);

            // Restore original model name
            this.chatModel.textContent = this.currentModel || 'Unknown Model';

            // Show error feedback with message
            this.showError(error.message);
        }
        
        this.closeDropdown();
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

// Escape HTML to prevent XSS in dynamic content
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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
        this.dropdownContent.innerHTML = this.extensions.map(ext => {
            const isActive = ext.id === this.currentExtensionId;
            const isDisabled = !this.freeSelection && !isActive && !this._isAdjacentLevel(ext.id);
            return `<div class="extension-dropdown-item${isActive ? ' active' : ''}${isDisabled ? ' disabled' : ''}"
                         data-extension-id="${ext.id}"
                         ${isDisabled ? '' : `onclick="window.extensionSelector.selectExtension(${ext.id})"`}>
                        <span class="extension-item-name">${escapeHtml(ext.name)}</span>
                        ${ext.description ? `<span class="extension-item-desc">${escapeHtml(ext.description)}</span>` : ''}
                    </div>`;
        }).join('');
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

    async selectExtension(extensionId) {
        if (extensionId === this.currentExtensionId) {
            this.closeDropdown();
            return;
        }
        try {
            const resp = await fetch(`/api/conversations/${currentConversationId}/extension`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ extension_id: extensionId })
            });
            if (!resp.ok) {
                const err = await resp.json();
                console.error('Extension switch failed:', err.detail);
                return;
            }
            this.currentExtensionId = extensionId;
            this.renderDropdown();
            this.updateCurrentDisplay();
            this.closeDropdown();
        } catch (e) {
            console.error('Extension switch error:', e);
        }
    }

    updateFromSSE(data) {
        if (data && data.id) {
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
            grouped[machine].sort((a, b) => a.model.localeCompare(b.model)).forEach(model => {
                const checked = this.selectedModels.some(s => s.llm_id === model.id) ? 'checked' : '';
                const safeModelName = escapeHtml(String(model.model || ''));
                const safeMachineName = escapeHtml(String(model.machine || ''));
                html += `
                    <label class="multi-ai-model-item">
                        <input type="checkbox" class="multi-ai-checkbox"
                               data-llm-id="${model.id}"
                               data-machine="${safeMachineName}"
                               data-model="${safeModelName}"
                               ${checked}>
                        <span class="multi-ai-model-name">${safeModelName}</span>
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
            document.getElementById('image-files').click();
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

    function isAdaptiveModel() {
        const model = (document.getElementById('chat-model')?.textContent || '').toLowerCase();
        return model.includes('4-6') || model.includes('4.6');
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
            currentThinkingBudget = parseInt(input.value) || 0;
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
