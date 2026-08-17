/* main.js */

// Model writes from the default dropdown and the conversation header must reach
// the server in user-event order. Aborting a fetch is not sufficient because the
// server may already have committed the write. Keep one shared queue and expose
// a pending barrier so messages cannot be sent against an older model identity.
if (typeof window.enqueueConversationModelMutation !== 'function') {
    let conversationModelMutationQueue = Promise.resolve();
    let conversationModelMutationGeneration = 0;
    let conversationModelMutationPendingCount = 0;

    window.conversationModelMutationPending = false;
    window.enqueueConversationModelMutation = function(task) {
        const generation = ++conversationModelMutationGeneration;
        conversationModelMutationPendingCount += 1;
        window.conversationModelMutationPending = true;

        const operation = conversationModelMutationQueue.then(
            () => Promise.resolve().then(task),
            () => Promise.resolve().then(task)
        );
        conversationModelMutationQueue = operation.catch(() => undefined);

        return operation.then(
            value => ({
                value,
                error: null,
                isLatest: generation === conversationModelMutationGeneration
            }),
            error => ({
                value: null,
                error,
                isLatest: generation === conversationModelMutationGeneration
            })
        ).finally(() => {
            conversationModelMutationPendingCount = Math.max(
                0,
                conversationModelMutationPendingCount - 1
            );
            window.conversationModelMutationPending =
                conversationModelMutationPendingCount > 0;
        });
    };
    window.invalidateConversationModelMutations = function() {
        conversationModelMutationGeneration += 1;
    };
}

document.addEventListener('DOMContentLoaded', function() {
    // Restore conversation ID after theme change reload
    const restoreConvId = localStorage.getItem('restoreConversationId');
    if (restoreConvId && typeof currentConversationId !== 'undefined') {
        currentConversationId = parseInt(restoreConvId, 10);
        localStorage.removeItem('restoreConversationId');
    }

    var fileInput = document.getElementById('chat-files');
    var previewsContainer = document.getElementById('image-previews');
    const dropZone = document.body; // Use whole body as drop zone
    const dropZoneOverlay = document.getElementById('drop-zone-overlay');
    var toggleButton = document.querySelector('.btn-toggle-sidebar');
    var sidebar = document.getElementById('sidebar');
    var userBalanceElement = document.getElementById("user-balance");
    const parsedUserBalance = userBalanceElement
        ? Number.parseFloat(userBalanceElement.getAttribute("data-balance") || '0')
        : 0;
    var userBalance = Number.isFinite(parsedUserBalance) ? parsedUserBalance : 0;
    var messageInputContainer = document.getElementById("form-message");
    var insufficientBalanceMessage = document.getElementById("insufficient-balance-message");
    var chatContent = document.querySelector('.chat-window');
    attachedFiles = []
    initFileHandling();
    const manager = new TextareaImagePreviewManager();

    // Save instance to window in case we need to access it later
    window.textareaImagePreviewManager = manager;
    const messageText = document.getElementById('message-text');

    messageText.addEventListener('input', function() {
        this.style.height = 'auto';
        this.style.height = this.scrollHeight + 'px';
    });
    // Theme initialization is now handled by themeManager.js

    // For the Prompt select
    var promptDropdown = document.getElementById('promptDropdown');
    if (promptDropdown) {
        promptDropdown.addEventListener('change', withSession(function(e) {
            const selectedValue = e.target.value;
            updateSelection('/chat', {
                form_type: 'prompt',
                prompt_id: selectedValue
            });
        }));
    } else {
        console.error('[DEBUG] Prompt dropdown NOT found');
    }

    // For the LLM select
    var llmDropdown = document.getElementById('llmDropdown');
    if (llmDropdown) {
        let committedLlmDropdownValue = llmDropdown.value;
        llmDropdown.addEventListener('change', function(e) {
            if (document.getElementById('send-button')?.innerText === 'Stop') {
                e.target.value = committedLlmDropdownValue;
                NotificationModal.warning(
                    'Message in progress',
                    'Wait for the current message to finish before changing the AI model.'
                );
                return;
            }
            const selectedValue = e.target.value;
            const selectedLlmId = parseInt(selectedValue, 10);
            const selectedConversationId =
                typeof currentConversationId !== 'undefined'
                    ? currentConversationId
                    : null;
            const selectedViewGeneration =
                typeof conversationViewGeneration !== 'undefined'
                    ? conversationViewGeneration
                    : null;

            window.enqueueConversationModelMutation(async () => {
                const defaultResult = await updateSelection('/chat', {
                    form_type: 'llm',
                    type_of_model: selectedValue
                });
                if (!defaultResult) {
                    throw new Error('Failed to update the default AI model');
                }
                if (!Number.isInteger(selectedLlmId) ||
                    !selectedConversationId) {
                    return { defaultResult, conversationResult: null };
                }

                // startNewConversation deliberately reuses an empty chat. Ask
                // the server to update that exact chat only if it is still empty;
                // this avoids stale client-side emptiness during navigation.
                let conversationResult;
                try {
                    conversationResult = await updateEmptyConversationSelection(
                        selectedConversationId,
                        selectedLlmId
                    );
                } catch (error) {
                    if (window.modelSelector) {
                        window.modelSelector.invalidateCachedModel(
                            selectedConversationId
                        );
                        await window.modelSelector.reconcileConversationModelIdentity(
                            selectedConversationId
                        );
                    }
                    throw error;
                }
                if (conversationResult?.updated && window.modelSelector) {
                    window.modelSelector.cacheCommittedModel(
                        selectedConversationId,
                        conversationResult.llm_id,
                        conversationResult.model
                    );
                    const targetIsCurrent =
                        typeof conversationIdsMatch === 'function'
                            ? conversationIdsMatch(
                                currentConversationId,
                                selectedConversationId
                            )
                            : String(currentConversationId) ===
                                String(selectedConversationId);
                    if (targetIsCurrent) {
                        window.modelSelector.applyCommittedModel(
                            conversationResult.llm_id,
                            conversationResult.model,
                            selectedConversationId,
                            conversationViewGeneration
                        );
                    }
                }
                return { defaultResult, conversationResult };
            }).then(({ value, error, isLatest }) => {
                if (error) {
                    if (isLatest) {
                        e.target.value = committedLlmDropdownValue;
                        console.error('Error updating model selection:', error);
                        NotificationModal.error(
                            'Model update failed',
                            'The selected AI model was not saved. Please try again.'
                        );
                    }
                    return;
                }
                if (value?.defaultResult) {
                    committedLlmDropdownValue = selectedValue;
                }
                const result = value?.conversationResult;
                const stillCurrent = selectedConversationId &&
                    (typeof isCurrentConversationView === 'function'
                        ? isCurrentConversationView(
                            selectedConversationId,
                            selectedViewGeneration
                        )
                        : String(currentConversationId) === String(selectedConversationId));
                if (!isLatest || !stillCurrent || !result?.updated ||
                    !window.modelSelector) {
                    return;
                }
                window.modelSelector.applyCommittedModel(
                    result.llm_id,
                    result.model,
                    selectedConversationId,
                    selectedViewGeneration
                );
            });
        });
    } else {
        console.error('[DEBUG] LLM dropdown NOT found');
    }

    // For code coloring
    hljs.highlightAll();

    function updateSelection(url, data) {
        return secureFetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        })
        .then(response => response.json())
        .then(data => {
            if (!data.success) {
                console.error('Error updating selection');
                return null;
            }
            return data;
        })
        .catch(error => {
            console.error('Error:', error);
            return null;
        });
    }

    async function updateEmptyConversationSelection(conversationId, llmId) {
        const response = await secureFetch(
            `/api/conversations/${conversationId}/model`,
            {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    llm_id: llmId,
                    only_if_empty: true
                })
            }
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.detail || 'Failed to update the empty conversation model');
        }
        if (data.success !== true || typeof data.updated !== 'boolean' ||
            (data.updated && (
                !Number.isInteger(parseInt(data.llm_id, 10)) ||
                typeof data.model !== 'string' || !data.model
            ))) {
            throw new Error('Empty conversation model update returned an invalid response');
        }
        return data;
    }
    window.updateConversationBalanceAvailability = function(isPaidOverride = null) {
        if (!messageInputContainer || !insufficientBalanceMessage) return;
        if (typeof admin_view !== 'undefined' && admin_view) return;

        const currentId = typeof currentConversationId !== 'undefined'
            ? currentConversationId
            : null;
        const selected = window.selectedChat &&
            (typeof conversationIdsMatch === 'function'
                ? conversationIdsMatch(
                    window.selectedChat.dataset?.conversationId,
                    currentId
                )
                : String(window.selectedChat.dataset?.conversationId) === String(currentId))
            ? window.selectedChat
            : null;
        const embedded = typeof embeddedInitialConversations !== 'undefined' &&
            Array.isArray(embeddedInitialConversations)
            ? embeddedInitialConversations.find(conversation =>
                (typeof conversationIdsMatch === 'function'
                    ? conversationIdsMatch(conversation.id, currentId)
                    : String(conversation.id) === String(currentId)))
            : null;
        const isPaid = typeof isPaidOverride === 'boolean'
            ? isPaidOverride
            : (selected?._conversationData?.is_paid === true ||
                selected?.dataset?.isPaid === '1' ||
                embedded?.is_paid === true);
        const hasByok = typeof hasOwnKeys !== 'undefined' && hasOwnKeys &&
            typeof apiKeyMode !== 'undefined' && apiKeyMode !== 'system_only';
        const usesGptSub = typeof window.currentConversationUsesChatGptSubscription === 'function' &&
            window.currentConversationUsesChatGptSubscription() === true;
        const hasOwnedCredential = hasByok || usesGptSub;
        const lacksPaidPromptBalance = hasOwnedCredential && isPaid && userBalance < 0.10;
        const lacksPlatformBalance = userBalance === 0 && !hasOwnedCredential;

        if (lacksPaidPromptBalance || lacksPlatformBalance) {
            messageInputContainer.style.display = 'none';
            insufficientBalanceMessage.style.display = 'block';
        } else {
            messageInputContainer.style.display = 'flex';
            insufficientBalanceMessage.style.display = 'none';
        }
    };

    function checkBalanceAndHideInput() {
        window.updateConversationBalanceAvailability();
    }
    checkBalanceAndHideInput();    

    function toggleSidebar() {
        sidebar.classList.toggle('active');
        dropZoneOverlay.classList.toggle('active');
    }

    function closeSidebar() {
        sidebar.classList.remove('active');
        dropZoneOverlay.classList.remove('active');
    }

    window.closeSidebar = closeSidebar;
    
    if (toggleButton) {
        toggleButton.addEventListener('click', function(event) {
            event.preventDefault();
            toggleSidebar();
        });
    } else {
        console.error('Toggle button not found');
    }

    if (dropZoneOverlay) {
        dropZoneOverlay.addEventListener('click', closeSidebar);
    } else {
        console.error('Overlay not found');
    }

    if (chatContent) {
        chatContent.addEventListener('click', function(event) {
            if (sidebar.classList.contains('active')) {
                closeSidebar();
            }
        });
    } else {
        console.error('Chat content not found');
    }

    let touchStartX = 0;
    let touchEndX = 0;

    document.addEventListener('touchstart', e => {
        if (isMobile()) {
            touchStartX = e.changedTouches[0].screenX;
        }
    });
    document.addEventListener('touchend', e => {
        if (isMobile()) {
            touchEndX = e.changedTouches[0].screenX;
            handleSwipe();
        }
    });

    function handleSwipe() {
        const swipeDistance = touchEndX - touchStartX;
        if (swipeDistance > 100 && !sidebar.classList.contains('active')) {
            toggleSidebar();
        } else if (swipeDistance < -100 && sidebar.classList.contains('active')) {
            closeSidebar();
        }
    }

    // Pull-to-refresh for mobile
    initPullToRefresh();

    // Close sidebar on mobile devices when loading
    if (isMobile()) {
        closeSidebar();
    }

    // Desktop sidebar collapse/expand
    var collapseSidebarBtn = document.getElementById('collapse-sidebar-btn');
    var expandSidebarBtn = document.getElementById('expand-sidebar-btn');

    function toggleDesktopSidebar() {
        var isCollapsed = document.documentElement.classList.toggle('sidebar-collapsed');
        try { localStorage.setItem('sidebarCollapsed', isCollapsed ? '1' : '0'); } catch(e) {}
    }

    if (collapseSidebarBtn) {
        collapseSidebarBtn.addEventListener('click', toggleDesktopSidebar);
    }
    if (expandSidebarBtn) {
        expandSidebarBtn.addEventListener('click', toggleDesktopSidebar);
    }

    // Show loading indicator at start
    document.getElementById('loading-indicator').style.display = 'block';

    // Load conversations
    loadConversations(false, true).then(() => {
        // Enable controls and hide loading indicator when all initial loads are complete
        enableInputControls();
        document.getElementById('loading-indicator').style.display = 'none';

        // Auto-start new conversation if redirected from explore page
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get('autostart') === '1') {
            window.history.replaceState({}, '', window.location.pathname);
            startNewConversation();
        }

        // Handle navigation from the home page via sessionStorage keys
        const homeStartPromptId = sessionStorage.getItem('home_start_prompt_id');
        const homeStartMessage = sessionStorage.getItem('home_start_message');
        const homeOpenConversationId = sessionStorage.getItem('home_open_conversation_id');

        if (homeStartPromptId) {
            sessionStorage.removeItem('home_start_prompt_id');
            sessionStorage.removeItem('home_open_conversation_id');
            sessionStorage.removeItem('home_start_message');
            startNewConversation(homeStartPromptId).then(() => {
                if (homeStartMessage) {
                    sendMessage(homeStartMessage);
                }
            });
        } else if (homeOpenConversationId) {
            sessionStorage.removeItem('home_open_conversation_id');
            const convId = parseInt(homeOpenConversationId, 10);
            const convElement = document.querySelector(`[data-conversation-id="${convId}"]`);
            if (convElement) {
                // Conversation is already in the sidebar list
                const chatName = convElement.querySelector('.chat-name')
                    ? convElement.querySelector('.chat-name').textContent.trim()
                    : 'Chat ' + convId;
                const machine = convElement.getAttribute('data-machine');
                convElement.classList.add('active-chat');
                continueConversation(convId, chatName, machine);
            } else {
                // Conversation not in sidebar (e.g. inside a folder or beyond loaded range)
                fetch('/api/conversations/' + convId + '/details')
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        continueConversation(convId, data.chat_name || 'Chat ' + convId, data.machine || 'machine');
                    })
                    .catch(function(err) {
                        console.error('Failed to open conversation from home:', err);
                    });
            }
        }
    }).catch(error => {
        console.error('Error during initial load:', error);
        // Make sure to enable controls even if there's an error
        enableInputControls();
        document.getElementById('loading-indicator').style.display = 'none';
    });

    if (Config.adminView) {
        var sendButton = document.querySelector('#form-message button[type="submit"]');
        messageText.classList.add('hidden');
        sendButton.classList.add('hidden');
    }

    // New code for "New Chat" split button
    var newChatMainBtn = document.getElementById('new-chat-main-btn');

    if (newChatMainBtn) {
        newChatMainBtn.addEventListener('click', function(e) {
            e.preventDefault();

            // Close the dropdown menu if it's open
            var dropdownToggle = document.querySelector('.btn-group .dropdown-toggle-split[data-bs-toggle="dropdown"]');
            if (dropdownToggle && typeof bootstrap !== 'undefined') {
                var bsDropdown = bootstrap.Dropdown.getOrCreateInstance(dropdownToggle);
                bsDropdown.hide();
            }

            // Check session before creating new conversation (force check for critical action)
            SessionManager.validateSession(true).then((isValid) => {
                if (isValid) {
                    // Session is valid, create new conversation
                    startNewConversation();
                } else {
                    // Session invalid, modal already shown by validateSession
                }
            });
        });
    } else {
        console.error('Main New Chat button not found');
    }

    // Theme is now handled by themeManager.js

    document.getElementById('form-message').onsubmit = function(e) {
        e.preventDefault();
        // Prevent sending while AI is streaming
        const sendButton = document.getElementById('send-button');
        if (sendButton.innerText === 'Stop') return;
        var messageText = document.getElementById('message-text').value;
        /*messageText = encodeForHTML(messageText);*/
        
        // Check session before sending message (force check for critical action)
        SessionManager.validateSession(true).then((isValid) => {
            if (isValid) {
                // Session is valid, send message and clear form
                try {
                    const didSend = sendMessage(messageText);
                    if (didSend) {
                        document.getElementById('message-text').value = '';
                        Config.attachedFiles = [];
                    }
                } catch (error) {
                    console.error('Error sending message:', error);
                    // Don't clear form if there was an error
                }
            } else {
                // Session invalid, modal already shown by validateSession
                // Don't clear the form so user can copy their message
            }
        });
    };

    document.addEventListener('keydown', function(event) {
        if (event.key === 'Escape') {
            const sendButton = document.getElementById('send-button');
            if (sendButton.innerText === 'Stop') {
                stopReceivingStream();
            } else {
                const imagePreviews = document.getElementById('image-previews');
                const previewItems = imagePreviews.querySelectorAll('img, .pdf-preview-item, .text-preview-item');
                if (previewItems.length > 0) {
                    const lastItem = previewItems[previewItems.length - 1];
                    imagePreviews.removeChild(lastItem);
                    attachedFiles.pop();
                    if (previewItems.length === 1) {
                        imagePreviews.classList.add('hidden');
                    }
                }
            }
        }
    });

    document.getElementById('message-text').addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            document.getElementById('form-message').dispatchEvent(new Event('submit', {cancelable: true}));
        }
    });

    let dragTimeout;

    if (dropZone) {
        dropZone.addEventListener('dragover', function(e) {
            e.preventDefault();
            e.stopPropagation();
            clearTimeout(dragTimeout);
            dropZoneOverlay.classList.remove('d-none');
            dropZoneOverlay.classList.add('d-flex');
        });
    
        dropZone.addEventListener('dragleave', function(e) {
            e.preventDefault();
            e.stopPropagation();
            dragTimeout = setTimeout(function() {
                dropZoneOverlay.classList.add('d-none');
                dropZoneOverlay.classList.remove('d-flex');
            }, 100);
        });
    
        dropZone.addEventListener('drop', function(e) {
            e.preventDefault();
            e.stopPropagation();
            clearTimeout(dragTimeout);
            dropZoneOverlay.classList.add('d-none');
            dropZoneOverlay.classList.remove('d-flex');
            if (!Config.can_send_files) return;

            const files = e.dataTransfer.files;
            handleDroppedFiles(files);
            setTimeout(() => {
                document.getElementById('message-text').focus();
            }, 0);
        });
    }
    
    async function handleDroppedFiles(files) {
        const imagePreviews = document.getElementById('image-previews');

        compressionInProgress++;
        try {
            let compressedCount = 0;
            let totalSavedBytes = 0;

            for (const file of files) {
                if (!isAcceptedFileType(file) || !validateFileSize(file)) continue;

                if (file.type === 'application/pdf') {
                    const pdfPreview = document.createElement('div');
                    pdfPreview.className = 'pdf-preview-item';
                    const iconSpan = document.createElement('span');
                    iconSpan.className = 'pdf-icon';
                    iconSpan.textContent = 'PDF';
                    const nameSpan = document.createElement('span');
                    nameSpan.className = 'pdf-name';
                    nameSpan.textContent = file.name;
                    pdfPreview.appendChild(iconSpan);
                    pdfPreview.appendChild(nameSpan);
                    trackAttachmentPreview(file, pdfPreview);
                    imagePreviews.appendChild(pdfPreview);
                    attachedFiles.push(file);
                } else if (isTextFile(file)) {
                    const previewItem = document.createElement('div');
                    previewItem.className = 'text-preview-item';
                    const icon = document.createElement('span');
                    icon.className = 'text-icon';
                    icon.textContent = 'TXT';
                    const name = document.createElement('span');
                    name.className = 'text-name';
                    name.textContent = file.name;
                    previewItem.appendChild(icon);
                    previewItem.appendChild(name);
                    trackAttachmentPreview(file, previewItem);
                    imagePreviews.appendChild(previewItem);
                    attachedFiles.push(file);
                } else if (file.type.startsWith('image/')) {
                    const processed = await maybeCompressImage(file);
                    attachedFiles.push(processed);

                    const reader = new FileReader();
                    reader.onload = function(e) {
                        if (!attachmentIsStillQueued(processed)) return;
                        const img = document.createElement('img');
                        img.src = e.target.result;
                        img.className = 'preview-image';
                        trackAttachmentPreview(processed, img);
                        imagePreviews.appendChild(img);
                    };
                    reader.readAsDataURL(processed);

                    if (processed !== file && processed.size < file.size) {
                        compressedCount++;
                        totalSavedBytes += file.size - processed.size;
                    }
                }
            }

            if (compressedCount > 0) {
                const saved = (totalSavedBytes / (1024 * 1024)).toFixed(1);
                const msg = compressedCount === 1
                    ? `Image compressed (saved ${saved} MB)`
                    : `${compressedCount} images compressed (saved ${saved} MB)`;
                NotificationModal.toast(msg, 'info', 3000);
            }
        } finally {
            compressionInProgress--;
        }

        if (attachedFiles.length > 0) {
            imagePreviews.classList.remove('hidden');
        }
        setTimeout(() => {
            document.getElementById('message-text').focus();
        }, 0);
    }
});

let attachedFiles = [];

function checkSession(callback) {
    fetch('/api/check-session')
        .then(response => {
            return response.json();
        })
        .then(data => {
            if (data.expired) {
                window.location.href = '/login';
            } else {
                callback();
            }
        })
        .catch(error => {
            console.error("Error checking session:", error);
        });
}

function isMobile() {
    return window.innerWidth < 769;
}

// This function is defined in chat.js and overwritten when that file loads
function startNewConversation() {
    // Stub function - overwritten by chat.js
}

class TextareaImagePreviewManager {
    constructor() {
        this.textarea = document.getElementById('message-text');
        this.previewsContainer = document.getElementById('image-previews');
        this.messageInput = document.querySelector('.message-input');
        this.formMessage = document.getElementById('form-message');
        
        if (!this.textarea || !this.previewsContainer || !this.messageInput) {
            console.error('Required elements not found');
            return;
        }
        
        this.init();
    }
    
    init() {
        // Set up initial styles
        this.setupStyles();

        // Set up observers
        this.setupObservers();

        // Set up event listeners
        this.setupEventListeners();

        // Initial update
        this.updatePreviewsPosition();
    }

    setupStyles() {
        // Styles for message container
        Object.assign(this.messageInput.style, {
            position: 'relative',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'flex-end'
        });

        // Previews container: only set transition for JS-controlled bottom positioning.
        // All visual styles (background, border, sizing, layout) come from each theme's CSS.
        Object.assign(this.previewsContainer.style, {
            transition: 'bottom 0.2s ease'
        });

        // Styles for the form
        if (this.formMessage) {
            Object.assign(this.formMessage.style, {
                width: '100%',
                maxWidth: '48rem',
                margin: '0 auto',
                position: 'relative'
            });
        }

        // Ensure textarea has necessary styles
        Object.assign(this.textarea.style, {
            maxHeight: '33vh',
            minHeight: '44px',
            overflowY: 'auto',
            resize: 'none',
            lineHeight: '1.4'
        });
    }

    setupObservers() {
        // Observe changes in textarea size
        this.resizeObserver = new ResizeObserver(() => {
            this.updatePreviewsPosition();
        });
        this.resizeObserver.observe(this.textarea);

        // Observe changes in previews container
        this.mutationObserver = new MutationObserver(() => {
            this.updatePreviewsPosition();
            this.updatePreviewsVisibility();
        });
        
        this.mutationObserver.observe(this.previewsContainer, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['class']
        });
    }

    setupEventListeners() {
        // Update on window resize
        window.addEventListener('resize', () => this.updatePreviewsPosition());

        // Update when typing
        this.textarea.addEventListener('input', () => this.updatePreviewsPosition());

        // Update when pasting content
        this.textarea.addEventListener('paste', () => {
            setTimeout(() => this.updatePreviewsPosition(), 0);
        });
    }
    
    updatePreviewsPosition() {
        if (this.previewsContainer.classList.contains('hidden')) {
            return;
        }

        // Position preview just above the entire message-input area.
        // #image-previews is a sibling of .message-input inside #window-chat (position: relative),
        // so bottom must account for the full height of .message-input, not just the textarea.
        const messageInputHeight = this.messageInput.offsetHeight;
        const bottomPosition = messageInputHeight + 4;

        this.previewsContainer.style.bottom = `${bottomPosition}px`;
    }
    
    updatePreviewsVisibility() {
        const hasImages = this.previewsContainer.children.length > 0;
        const isHidden = this.previewsContainer.classList.contains('hidden');
        
        if (hasImages && isHidden) {
            this.previewsContainer.classList.remove('hidden');
            this.updatePreviewsPosition();
        } else if (!hasImages && !isHidden) {
            this.previewsContainer.classList.add('hidden');
        }
    }

    // Method to clean up observers if necessary
    destroy() {
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
        }
        if (this.mutationObserver) {
            this.mutationObserver.disconnect();
        }
    }
}

function updatePreviewsPosition() {
    if (window.textareaImagePreviewManager) {
        window.textareaImagePreviewManager.updatePreviewsPosition();
    }
}

/**
 * Pull-to-refresh for mobile devices
 * Allows refreshing the chat by pulling down when at the top of the messages
 */
function initPullToRefresh() {
    if (!isMobile()) return;

    const chatWindow = document.getElementById('chat-window');
    if (!chatWindow) return;

    // Create the pull-to-refresh indicator
    const ptrIndicator = document.createElement('div');
    ptrIndicator.id = 'ptr-indicator';
    ptrIndicator.innerHTML = '<div class="ptr-spinner"></div><span class="ptr-text">Pull to refresh</span>';

    // Inject styles
    const style = document.createElement('style');
    style.textContent = `
        #ptr-indicator {
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            padding: 15px;
            background: var(--chat-window-bg-color, #36393f);
            color: var(--text-secondary, #b9bbbe);
            font-size: 14px;
            transform: translateY(-100%);
            transition: transform 0.2s ease, opacity 0.2s ease;
            z-index: 100;
            opacity: 0;
            pointer-events: none;
        }
        #ptr-indicator.visible {
            opacity: 1;
        }
        #ptr-indicator.refreshing .ptr-spinner {
            animation: ptr-spin 0.8s linear infinite;
        }
        #ptr-indicator.refreshing .ptr-text {
            display: none;
        }
        .ptr-spinner {
            width: 20px;
            height: 20px;
            border: 2px solid var(--text-muted, rgba(255,255,255,0.3));
            border-top-color: var(--accent, #7289da);
            border-radius: 50%;
            transition: transform 0.1s ease;
        }
        .ptr-text {
            font-weight: 500;
        }
        @keyframes ptr-spin {
            to { transform: rotate(360deg); }
        }
    `;
    document.head.appendChild(style);
    chatWindow.style.position = 'relative';
    chatWindow.insertBefore(ptrIndicator, chatWindow.firstChild);

    // Pull-to-refresh state
    let touchStartY = 0;
    let pullDistance = 0;
    let isPulling = false;
    let isRefreshing = false;
    const THRESHOLD = 80;
    const MAX_PULL = 120;

    chatWindow.addEventListener('touchstart', (e) => {
        if (isRefreshing) return;
        if (chatWindow.scrollTop <= 0) {
            touchStartY = e.touches[0].clientY;
            isPulling = true;
        }
    }, { passive: true });

    chatWindow.addEventListener('touchmove', (e) => {
        if (!isPulling || isRefreshing) return;

        const touchY = e.touches[0].clientY;
        pullDistance = Math.min(touchY - touchStartY, MAX_PULL);

        if (pullDistance > 0 && chatWindow.scrollTop <= 0) {
            // Prevent default scroll when pulling
            e.preventDefault();

            // Update indicator position and appearance
            const progress = Math.min(pullDistance / THRESHOLD, 1);
            ptrIndicator.style.transform = `translateY(${pullDistance - ptrIndicator.offsetHeight}px)`;
            ptrIndicator.classList.add('visible');

            // Rotate spinner based on pull distance
            const spinner = ptrIndicator.querySelector('.ptr-spinner');
            spinner.style.transform = `rotate(${progress * 180}deg)`;

            // Update text
            const text = ptrIndicator.querySelector('.ptr-text');
            text.textContent = pullDistance >= THRESHOLD ? 'Release to refresh' : 'Pull to refresh';
        }
    }, { passive: false });

    chatWindow.addEventListener('touchend', async () => {
        if (!isPulling || isRefreshing) return;

        if (pullDistance >= THRESHOLD) {
            // Trigger refresh
            isRefreshing = true;
            ptrIndicator.classList.add('refreshing');
            ptrIndicator.style.transform = 'translateY(0)';

            try {
                if (typeof window.refreshActiveConversation === 'function') {
                    await window.refreshActiveConversation();
                } else {
                    // Fallback: reload messages if refreshActiveConversation is not available
                    if (typeof loadMessages === 'function' && typeof currentConversationId !== 'undefined' && currentConversationId) {
                        await loadMessages(currentConversationId, false);
                    }
                }
            } catch (error) {
                console.error('Pull-to-refresh error:', error);
            }

            // Small delay before hiding to show completion
            setTimeout(() => {
                hideIndicator();
                isRefreshing = false;
            }, 300);
        } else {
            hideIndicator();
        }

        isPulling = false;
        pullDistance = 0;
    }, { passive: true });

    function hideIndicator() {
        ptrIndicator.style.transform = 'translateY(-100%)';
        ptrIndicator.classList.remove('visible', 'refreshing');
    }
}
