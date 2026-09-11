/* message-search.js — Message search UI (WhatsApp-style sidebar results) */

const messageSearchState = {
    query: '',
    items: [],
    offset: 0,
    hasMore: false,
    selectedResultId: null,
    active: false,
    abortController: null,
    highlightedMessageId: null,
    sidebarDisplayCache: null
};

function initMessageSearch() {
    const input = document.getElementById('message-search-input');
    const clearBtn = document.getElementById('search-clear-btn');
    const loadMoreBtn = document.getElementById('search-load-more');

    if (!input) return;

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const q = input.value.trim();
            if (q.length >= 3) {
                messageSearchState.query = q;
                executeMessageSearch(true);
            }
        }
    });

    input.addEventListener('input', () => {
        clearBtn.classList.toggle('visible', input.value.length > 0);
        if (input.value.trim() === '' && messageSearchState.active) {
            deactivateSearchMode();
        }
    });

    clearBtn.addEventListener('click', () => {
        input.value = '';
        clearBtn.classList.remove('visible');
        deactivateSearchMode();
        input.focus();
    });

    loadMoreBtn.addEventListener('click', () => {
        executeMessageSearch(false);
    });
}

async function executeMessageSearch(reset = true) {
    // Abort previous request
    if (messageSearchState.abortController) {
        messageSearchState.abortController.abort();
    }
    messageSearchState.abortController = new AbortController();

    if (reset) {
        messageSearchState.items = [];
        messageSearchState.offset = 0;
        messageSearchState.hasMore = false;
        messageSearchState.selectedResultId = null;
    }

    activateSearchMode();

    const params = new URLSearchParams({
        q: messageSearchState.query,
        limit: '30',
        offset: String(messageSearchState.offset)
    });

    try {
        const response = await secureFetch(`/api/messages/search?${params}`, {
            signal: messageSearchState.abortController.signal
        });

        if (!response) return; // session expired

        if (!response.ok) {
            if (response.status === 422) {
                // Validation error (query too short, etc.)
                renderSearchResults(false);
                return;
            }
            console.error('Search failed:', response.status);
            return;
        }

        const data = await response.json();

        if (reset) {
            messageSearchState.items = data.items;
        } else {
            messageSearchState.items = messageSearchState.items.concat(data.items);
        }
        messageSearchState.hasMore = data.has_more;
        messageSearchState.offset = data.next_offset;

        renderSearchResults(!reset);
    } catch (err) {
        if (err.name === 'AbortError') return;
        console.error('Search error:', err);
    }
}

function renderSearchResults(append = false) {
    const list = document.getElementById('search-results-list');
    const countEl = document.getElementById('search-results-count');
    const loadMoreBtn = document.getElementById('search-load-more');
    const emptyState = document.getElementById('search-empty-state');

    if (!append) {
        list.innerHTML = '';
    }

    const items = append
        ? messageSearchState.items.slice(messageSearchState.items.length - 30)
        : messageSearchState.items;

    items.forEach((item) => {
        const div = document.createElement('div');
        div.className = 'search-result-item';
        div.setAttribute('role', 'option');
        div.dataset.messageId = item.message_id;
        div.dataset.conversationId = item.conversation_id;

        if (item.message_id === messageSearchState.selectedResultId) {
            div.classList.add('active-result');
        }

        const header = document.createElement('div');
        header.className = 'search-result-header';
        const chatName = document.createElement('span');
        chatName.className = 'search-result-chat-name';
        chatName.textContent = item.chat_name;
        const meta = document.createElement('span');
        meta.className = 'search-result-meta';
        const type = document.createElement('span');
        type.className = 'search-result-type';
        type.textContent = item.type;
        const date = document.createElement('span');
        date.textContent = formatSearchDate(item.date);
        meta.append(type, date);
        header.append(chatName, meta);
        const snippet = document.createElement('div');
        snippet.className = 'search-result-snippet';
        // The API supplies marked-up snippets; all other result fields remain text nodes.
        snippet.innerHTML = item.snippet_html;
        div.append(header, snippet);

        div.addEventListener('click', () => openSearchResult(item));
        list.appendChild(div);
    });

    const total = messageSearchState.items.length;
    countEl.textContent = AurvekI18n.t('chat_widgets.search.results', {
        count: total,
        suffix: messageSearchState.hasMore ? '+' : ''
    });

    loadMoreBtn.style.display = messageSearchState.hasMore ? 'block' : 'none';
    emptyState.style.display = total === 0 ? 'block' : 'none';
}

function activateSearchMode() {
    const wasAlreadyActive = messageSearchState.active;
    messageSearchState.active = true;

    const resultsSection = document.getElementById('search-results-section');
    resultsSection.classList.add('active');

    // Hide normal sidebar sections
    const sidebar = resultsSection.parentElement;
    const sections = sidebar.querySelectorAll('.sidebar-section');
    if (!wasAlreadyActive) {
        messageSearchState.sidebarDisplayCache = Array.from(sections).map(section => ({
            section,
            display: section.style.display
        }));
    }
    sections.forEach(section => {
        section.style.display = 'none';
    });

    // Keep search section visible
    const searchSection = sidebar.querySelector('.search-section');
    if (searchSection) searchSection.style.display = '';
}

function deactivateSearchMode() {
    messageSearchState.active = false;
    messageSearchState.query = '';
    messageSearchState.items = [];
    messageSearchState.offset = 0;
    messageSearchState.hasMore = false;
    messageSearchState.selectedResultId = null;

    if (messageSearchState.abortController) {
        messageSearchState.abortController.abort();
        messageSearchState.abortController = null;
    }

    clearPersistentHighlight();

    const resultsSection = document.getElementById('search-results-section');
    resultsSection.classList.remove('active');

    // Show all sidebar sections again
    const sidebar = resultsSection.parentElement;
    if (Array.isArray(messageSearchState.sidebarDisplayCache) && messageSearchState.sidebarDisplayCache.length > 0) {
        messageSearchState.sidebarDisplayCache.forEach(({ section, display }) => {
            if (section && section.isConnected) {
                section.style.display = display || '';
            }
        });
    } else {
        const sections = sidebar.querySelectorAll('.sidebar-section');
        sections.forEach(section => {
            section.style.display = '';
        });
    }
    messageSearchState.sidebarDisplayCache = null;

    // Ensure external section visibility follows actual external chat count.
    if (typeof updateExternalSection === 'function') {
        updateExternalSection();
    }
}

function openSearchResult(item) {
    clearPersistentHighlight();

    messageSearchState.selectedResultId = item.message_id;

    // Update active state in result list
    document.querySelectorAll('.search-result-item').forEach(el => {
        el.classList.toggle('active-result',
            parseInt(el.dataset.messageId) === item.message_id);
    });

    continueConversation(
        item.conversation_id,
        item.chat_name,
        null,
        false,
        item.message_id
    );

    // Poll for the target message element and highlight it directly.
    // The internal chain (loadMessages -> highlightAndScrollToMessage) handles
    // scrollIntoView, but we ensure the persistent highlight here.
    waitForMessageAndHighlight(item.message_id);
}

function getChatMessageElement(messageId) {
    const chatMessagesContainer = document.getElementById('chat-messages-container');
    if (!chatMessagesContainer) return null;
    return chatMessagesContainer.querySelector(`.message[data-message-id="${messageId}"]`);
}

function ensureSearchHighlightDismissButton(messageEl) {
    if (!messageEl) return;

    let dismissBtn = messageEl.querySelector('.message-highlight-dismiss');
    if (!dismissBtn) {
        dismissBtn = document.createElement('button');
        dismissBtn.type = 'button';
        dismissBtn.className = 'message-highlight-dismiss';
        dismissBtn.setAttribute('aria-label', AurvekI18n.t('chat_widgets.search.clear_highlight'));
        dismissBtn.title = AurvekI18n.t('chat_widgets.search.clear_highlight');
        dismissBtn.textContent = 'x';

        dismissBtn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            clearPersistentHighlight();
        });

        messageEl.appendChild(dismissBtn);
    }
}

function removeSearchHighlightDismissButton(messageEl) {
    if (!messageEl) return;
    const dismissBtn = messageEl.querySelector('.message-highlight-dismiss');
    if (dismissBtn) dismissBtn.remove();
}

function waitForMessageAndHighlight(messageId, maxAttempts = 60) {
    let attempts = 0;
    const interval = setInterval(() => {
        attempts++;
        const el = getChatMessageElement(messageId);
        if (el) {
            clearInterval(interval);
            clearPersistentHighlight();
            el.classList.add('highlight-persistent');
            ensureSearchHighlightDismissButton(el);
            messageSearchState.highlightedMessageId = messageId;
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else if (attempts >= maxAttempts) {
            clearInterval(interval);
        }
    }, 150);
}

function clearPersistentHighlight() {
    if (messageSearchState.highlightedMessageId) {
        const prev = getChatMessageElement(messageSearchState.highlightedMessageId);
        if (prev) {
            prev.classList.remove('highlight-persistent');
            removeSearchHighlightDismissButton(prev);
        }
        messageSearchState.highlightedMessageId = null;
    }
    // Also clean any stray persistent highlights in the message list
    document.querySelectorAll('#chat-messages-container .message.highlight-persistent').forEach(el => {
        el.classList.remove('highlight-persistent');
        removeSearchHighlightDismissButton(el);
    });
    document.querySelectorAll('#chat-messages-container .message .message-highlight-dismiss').forEach(el => {
        el.remove();
    });
}

function formatSearchDate(dateStr) {
    try {
        const d = new Date(dateStr + 'Z'); // UTC
        const now = new Date();
        const diffDays = Math.floor((now - d) / 86400000);
        if (diffDays === 0) {
            return d.toLocaleTimeString(AurvekI18n.locale, { hour: '2-digit', minute: '2-digit' });
        } else if (diffDays < 7) {
            return d.toLocaleDateString(AurvekI18n.locale, { weekday: 'short' });
        } else {
            return d.toLocaleDateString(AurvekI18n.locale, { month: 'short', day: 'numeric' });
        }
    } catch {
        return dateStr;
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

document.addEventListener('DOMContentLoaded', initMessageSearch);

// Used by chat.js when persistent highlight is applied from there.
window.ensureSearchHighlightDismissButton = ensureSearchHighlightDismissButton;
