/* folders.js - Chat Folders Management */

let chatFolders = [];
let currentEditingFolderId = null;
let currentMovingConversationId = null;
let currentSelectedFolderId = null; // Track currently selected folder for new chat creation

// Per-folder cursor pagination state: { lastActivity: string|null, lastId: number|null, allLoaded: boolean, loading: boolean }
const folderPaginationState = new Map();
const FOLDER_CHATS_PAGE_SIZE = 5;

// Folder-level pagination (client-side, all folders loaded but rendered in batches)
const FOLDERS_INITIAL_LIMIT = 10;
const FOLDERS_LOAD_MORE_SIZE = 25;
let foldersDisplayedCount = 0;

// Initialize folders functionality
async function initializeFolders() {
    await loadChatFolders();
    setupFoldersEventListeners();
    setupDragAndDrop();
}

// Setup event listeners for folders
function setupFoldersEventListeners() {
    // Add folder button
    document.getElementById('add-folder-header-btn')?.addEventListener('click', showAddFolderModal);
    
    // Save folder button
    document.getElementById('saveFolderBtn')?.addEventListener('click', saveFolderHandler);
    
    // Color presets
    document.querySelectorAll('.color-preset').forEach(preset => {
        preset.addEventListener('click', (e) => {
            const color = e.target.dataset.color;
            document.getElementById('folderColor').value = color;
        });
    });
    
    // Remove from folder button
    document.getElementById('removeFromFolderBtn')?.addEventListener('click', removeFromFolderHandler);
}

// Show add folder modal
function showAddFolderModal() {
    currentEditingFolderId = null;
    document.getElementById('folderModalLabel').textContent = 'Add Folder';
    document.getElementById('folderName').value = '';
    document.getElementById('folderColor').value = '#3B82F6';
    
    const modal = new bootstrap.Modal(document.getElementById('folderModal'));
    modal.show();
}

// Show edit folder modal
function showEditFolderModal(folderId) {
    const folder = chatFolders.find(f => f.id === folderId);
    if (!folder) return;
    
    currentEditingFolderId = folderId;
    document.getElementById('folderModalLabel').textContent = 'Edit Folder';
    document.getElementById('folderName').value = folder.name;
    document.getElementById('folderColor').value = folder.color;
    
    const modal = new bootstrap.Modal(document.getElementById('folderModal'));
    modal.show();
}

// Save folder handler
const saveFolderHandler = withSession(async function() {
    const name = document.getElementById('folderName').value.trim();
    const color = document.getElementById('folderColor').value;
    
    if (!name) {
        NotificationModal.toast('Folder name is required', 'error');
        return;
    }
    
    try {
        let response;
        if (currentEditingFolderId) {
            // Edit existing folder
            response = await secureFetch(`/api/chat-folders/${currentEditingFolderId}`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ name, color })
            });
        } else {
            // Create new folder
            response = await secureFetch('/api/chat-folders', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ name, color })
            });
        }
        
        const result = await response.json();
        
        if (response.ok) {
            NotificationModal.toast(result.message || 'Folder saved successfully', 'success');
            await loadChatFolders();
            
            // Update existing chat menus
            updateAllChatMenus();
            
            // Close modal
            const modal = bootstrap.Modal.getInstance(document.getElementById('folderModal'));
            modal.hide();
        } else {
            NotificationModal.toast(result.error || 'Failed to save folder', 'error');
        }
    } catch (error) {
        console.error('Error saving folder:', error);
        NotificationModal.toast('Failed to save folder', 'error');
    }
});

// Load chat folders
async function loadChatFolders() {
    try {
        // Use embedded data if available (avoids HTTP request on initial load)
        if (typeof embeddedChatFolders !== 'undefined' && embeddedChatFolders !== null) {
            chatFolders = embeddedChatFolders;
            embeddedChatFolders = null; // Clear to avoid reuse on subsequent calls
            renderChatFolders();
            return true;
        }

        // Fallback to API fetch
        const response = await fetch('/api/chat-folders');
        const result = await response.json();

        if (response.ok) {
            chatFolders = result.folders || [];
            renderChatFolders();
            return true;
        } else {
            console.error('Failed to load folders:', result.error);
            return false;
        }
    } catch (error) {
        console.error('Error loading folders:', error);
        return false;
    }
}

// Render chat folders in sidebar (paginated: shows first batch, "Load more" for rest)
function renderChatFolders() {
    const container = document.getElementById('chat-folders-container');
    if (!container) return;

    container.innerHTML = '';
    foldersDisplayedCount = 0;

    const initialBatch = chatFolders.slice(0, FOLDERS_INITIAL_LIMIT);
    initialBatch.forEach(folder => {
        container.appendChild(createFolderElement(folder));
    });
    foldersDisplayedCount = initialBatch.length;

    if (chatFolders.length > FOLDERS_INITIAL_LIMIT) {
        appendFoldersLoadMoreButton(container);
    }
}

// Append "Load more" button for folders list
function appendFoldersLoadMoreButton(container) {
    const remaining = chatFolders.length - foldersDisplayedCount;
    const loadMoreBtn = document.createElement('button');
    loadMoreBtn.className = 'folders-load-more-btn';
    loadMoreBtn.innerHTML = `Load more (${remaining}) <i class="fas fa-chevron-down"></i>`;
    loadMoreBtn.onclick = loadMoreFolders;
    container.appendChild(loadMoreBtn);
}

// Load next batch of folders
function loadMoreFolders() {
    const container = document.getElementById('chat-folders-container');
    if (!container) return;

    // Remove existing "Load more" button
    const existingBtn = container.querySelector('.folders-load-more-btn');
    if (existingBtn) existingBtn.remove();

    const nextBatch = chatFolders.slice(foldersDisplayedCount, foldersDisplayedCount + FOLDERS_LOAD_MORE_SIZE);
    nextBatch.forEach(folder => {
        container.appendChild(createFolderElement(folder));
    });
    foldersDisplayedCount += nextBatch.length;

    if (foldersDisplayedCount < chatFolders.length) {
        appendFoldersLoadMoreButton(container);
    }
}

// Create folder element
function createFolderElement(folder) {
    const folderDiv = document.createElement('div');
    folderDiv.className = 'folder-item d-flex justify-content-between align-items-center mb-1 p-2 rounded';
    folderDiv.style.cursor = 'pointer';
    folderDiv.dataset.folderId = folder.id;
    folderDiv.dataset.folderExpanded = 'false';
    
    // Folder content (left side)
    const folderContent = document.createElement('div');
    folderContent.className = 'd-flex align-items-center flex-grow-1';
    
    // Get folder icon based on state
    const iconClass = folderDiv.dataset.folderExpanded === 'true' ? 'fa-folder-open' : 'fa-folder';

    // Chat count for tooltip
    const chatCount = folder.conversation_count || 0;
    const chatCountText = chatCount === 1 ? '1 chat' : `${chatCount} chats`;

    folderContent.innerHTML = `
        <i class="fas fa-chevron-right folder-chevron"></i>
        <i class="fas ${iconClass} folder-icon me-2" style="color: ${folder.color}; font-size: 16px;"></i>
        <span class="folder-name">${escapeHTML(folder.name)}</span>
    `;

    // Set tooltip with chat count
    folderDiv.title = chatCountText;
    
    // Actions dropdown (right side) - now hidden by default, shown on hover
    const actionsDiv = document.createElement('div');
    actionsDiv.className = 'folder-actions dropdown';
    actionsDiv.innerHTML = `
        <button class="btn btn-sm btn-ghost dropdown-toggle" type="button" data-bs-toggle="dropdown" aria-expanded="false">
            <i class="fas fa-ellipsis-h"></i>
        </button>
        <ul class="dropdown-menu dropdown-menu-dark folder-dropdown-menu">
            <li><a class="dropdown-item" href="#" onclick="showEditFolderModal(${folder.id})">
                <i class="fas fa-edit"></i> Edit
            </a></li>
            <li><a class="dropdown-item text-danger" href="#" onclick="deleteFolderHandler(${folder.id})">
                <i class="fas fa-trash"></i> Delete
            </a></li>
        </ul>
    `;

    // Raise folder z-index and unlock overflow when dropdown opens
    actionsDiv.addEventListener('shown.bs.dropdown', () => {
        const folderItem = actionsDiv.closest('.folder-item');
        if (folderItem) folderItem.style.zIndex = '10';
        const container = actionsDiv.closest('#chat-folders-container');
        if (container) container.style.overflow = 'visible';
    });
    actionsDiv.addEventListener('hidden.bs.dropdown', () => {
        const folderItem = actionsDiv.closest('.folder-item');
        if (folderItem) folderItem.style.zIndex = '';
        const container = actionsDiv.closest('#chat-folders-container');
        if (container) container.style.overflow = '';
    });

    // Click handler for folder (expand/collapse chats in folder)
    folderContent.addEventListener('click', () => toggleFolderChats(folder.id));
    
    folderDiv.appendChild(folderContent);
    folderDiv.appendChild(actionsDiv);
    
    // Add container for folder's chats (initially hidden)
    const folderChatsContainer = document.createElement('div');
    folderChatsContainer.className = 'folder-chats-container mt-2';
    folderChatsContainer.id = `folder-chats-${folder.id}`;
    folderChatsContainer.style.display = 'none';
    
    const folderWrapper = document.createElement('div');
    folderWrapper.appendChild(folderDiv);
    folderWrapper.appendChild(folderChatsContainer);
    
    return folderWrapper;
}

// Toggle folder chats visibility
function toggleFolderChats(folderId) {
    const container = document.getElementById(`folder-chats-${folderId}`);
    const folderItem = document.querySelector(`[data-folder-id="${folderId}"]`);
    if (!container || !folderItem) return;
    
    // Update selected folder tracking
    const wasExpanded = container.style.display !== 'none';
    
    // Collapse all other folders first and clear their pagination state
    document.querySelectorAll('.folder-chats-container').forEach(folderContainer => {
        folderContainer.style.display = 'none';
        const collapsedFolderId = folderContainer.id.replace('folder-chats-', '');
        if (collapsedFolderId) folderPaginationState.delete(Number(collapsedFolderId));
    });

    // Remove active styling from all folders and update icons
    document.querySelectorAll('.folder-item').forEach(item => {
        item.classList.remove('folder-selected');
        item.dataset.folderExpanded = 'false';
        const icon = item.querySelector('.folder-icon');
        if (icon) {
            icon.classList.remove('fa-folder-open');
            icon.classList.add('fa-folder');
        }
    });
    
    if (wasExpanded) {
        // If this folder was expanded, collapse it and deselect
        container.style.display = 'none';
        currentSelectedFolderId = null;
        folderPaginationState.delete(folderId);
        folderItem.dataset.folderExpanded = 'false';
        const icon = folderItem.querySelector('.folder-icon');
        if (icon) {
            icon.classList.remove('fa-folder-open');
            icon.classList.add('fa-folder');
        }
        updateNewChatButtonState();
    } else {
        // Expand this folder and select it
        container.style.display = 'block';
        currentSelectedFolderId = folderId;
        
        // Add active styling to selected folder and update icon
        folderItem.classList.add('folder-selected');
        folderItem.dataset.folderExpanded = 'true';
        const icon = folderItem.querySelector('.folder-icon');
        if (icon) {
            icon.classList.remove('fa-folder');
            icon.classList.add('fa-folder-open');
        }
        
        loadFolderChats(folderId);
        updateNewChatButtonState();
    }
}

// Update conversation count for a specific folder without reloading everything
async function updateFolderConversationCount(folderId) {
    try {
        const response = await secureFetch(`/api/conversations?user_id=${user_id}&folder_id=${folderId}&limit=50`);
        if (!response) {
            // secureFetch returned null (session expired, modal already shown)
            return;
        }
        const folderConversations = await response.json();
        
        // Find the folder element and update its count
        const folderElement = document.querySelector(`[data-folder-id="${folderId}"]`);
        if (folderElement) {
            const countElement = folderElement.querySelector('.conversation-count');
            if (countElement) {
                countElement.textContent = `(${folderConversations.length})`;
            }
        }
    } catch (error) {
        if (error.message === 'Session expired') {
            // Session validation was already handled by secureFetch, no need to log as error
        } else {
            console.error('Error updating folder conversation count:', error);
        }
    }
}

// Load chats for a specific folder with cursor pagination (sorted by last_activity)
async function loadFolderChats(folderId, onComplete = null, cursorParams = null) {
    const container = document.getElementById(`folder-chats-${folderId}`);
    if (!container) return;

    // In-flight guard via pagination state
    let state = folderPaginationState.get(folderId);
    if (state && state.loading) return;

    const isFirstPage = (cursorParams === null);

    if (isFirstPage) {
        // Reset state and clear container for fresh load
        state = { lastActivity: null, lastId: null, allLoaded: false, loading: true };
        folderPaginationState.set(folderId, state);
        container.innerHTML = '';
    } else {
        if (!state) return;
        state.loading = true;
        // Remove existing "Load more" button before fetching next page
        const existingBtn = container.querySelector('.folder-load-more-btn');
        if (existingBtn) existingBtn.remove();
    }

    try {
        let url = `/api/conversations?user_id=${user_id}&folder_id=${folderId}&limit=${FOLDER_CHATS_PAGE_SIZE}`;
        if (cursorParams) {
            if (cursorParams.beforeActivity) url += `&before_activity=${encodeURIComponent(cursorParams.beforeActivity)}`;
            if (cursorParams.beforeId) url += `&before_id=${cursorParams.beforeId}`;
        }

        const response = await secureFetch(url);
        if (!response) return; // secureFetch returned null (session expired)

        const conversations = await response.json();

        if (isFirstPage && conversations.length === 0) {
            const emptyMsg = document.createElement('div');
            emptyMsg.className = 'folder-empty-msg';
            emptyMsg.textContent = 'Empty folder';
            container.appendChild(emptyMsg);
        } else {
            conversations.forEach(conversation => {
                const chatElement = createFolderChatElement(conversation);
                container.appendChild(chatElement);
            });

            // Update compound cursor from the last (oldest) conversation in the batch
            if (conversations.length > 0) {
                const lastConv = conversations[conversations.length - 1];
                state.lastActivity = lastConv.last_activity || null;
                state.lastId = Number(lastConv.id);
            }

            // Determine if all items have been loaded
            if (conversations.length < FOLDER_CHATS_PAGE_SIZE) {
                state.allLoaded = true;
            } else {
                // Append "Load more" button
                const loadMoreBtn = document.createElement('button');
                loadMoreBtn.className = 'folder-load-more-btn';
                loadMoreBtn.innerHTML = 'Load more <i class="fas fa-chevron-down"></i>';
                loadMoreBtn.onclick = () => loadMoreFolderChats(folderId);
                container.appendChild(loadMoreBtn);
            }
        }

        // Execute callback if provided (after DOM is updated)
        if (onComplete && typeof onComplete === 'function') {
            onComplete();
        }
    } catch (error) {
        if (error.message === 'Session expired') {
            // Already handled by secureFetch
        } else {
            console.error('Error loading folder chats:', error);
        }
    } finally {
        if (state) state.loading = false;
    }
}

// Load next page of chats for a folder
function loadMoreFolderChats(folderId) {
    const state = folderPaginationState.get(folderId);
    if (!state || state.allLoaded || state.loading) return;
    loadFolderChats(folderId, null, { beforeActivity: state.lastActivity, beforeId: state.lastId });
}

// Create chat element for folder
function createFolderChatElement(conversation) {
    const chatElement = document.createElement('a');
    chatElement.href = '#';
    chatElement.className = 'list-group-item list-group-item-action folder-chat-item';
    chatElement.dataset.conversationId = conversation.id;
    chatElement.dataset.lastActivity = conversation.last_activity || '';

    const chatName = conversation.chat_name || `Chat ${conversation.id}`;
    
    // Create the chat content
    const chatContent = document.createElement('div');
    chatContent.className = 'chat-content-container d-flex justify-content-between align-items-center';
    
    const chatInfo = document.createElement('div');
    chatInfo.className = 'chat-content';
    chatInfo.innerHTML = `
        <div class="chat-name">${escapeHTML(chatName)}</div>
        <small class="text-muted">${new Date(conversation.start_date).toLocaleDateString()}</small>
    `;
    
    // Create the complete chat menu (same as main chats)
    const chatMenu = createChatMenuForFolder(conversation);
    
    chatContent.appendChild(chatInfo);
    chatContent.appendChild(chatMenu);
    chatElement.appendChild(chatContent);

    // Make folder chat items draggable (to move out of folder or between folders)
    chatElement.setAttribute('draggable', 'true');
    chatElement.style.cursor = 'grab';
    chatElement.addEventListener('dragstart', handleDragStart);
    chatElement.addEventListener('dragend', handleDragEnd);

    // Click handler to continue conversation
    chatElement.addEventListener('click', (e) => {
        if (!e.target.closest('.chat-menu')) {
            // Remove active-chat class from ALL chats everywhere
            document.querySelectorAll('.active-chat').forEach(el => {
                el.classList.remove('active-chat');
            });
            
            // Add active-chat class to this element
            chatElement.classList.add('active-chat');
            
            // Update selectedChat global variable so active chat detection works
            window.selectedChat = chatElement;
            
            continueConversation(conversation.id, chatName);
        }
    });
    
    return chatElement;
}

// Create chat menu for folder chats (similar to main chat menu)
function createChatMenuForFolder(conversation) {
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

    // Download MP3 option
    const downloadAudioLink = createMenuLink('fa-music', 'Download MP3', () => downloadAudio(conversation.id));
    chatMenuContent.appendChild(downloadAudioLink);

    // Download PDF option
    const downloadPdfLink = createMenuLink('fa-download', 'Download PDF', () => downloadPDF(conversation.id));
    chatMenuContent.appendChild(downloadPdfLink);

    // Delete option
    const deleteLink = createMenuLink('fa-trash-alt', 'Delete', () => deleteConversation(conversation.id), 'text-danger');
    chatMenuContent.appendChild(deleteLink);

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

    // Add folder options section
    addFolderOptionsToMenu(chatMenuContent, conversation.id, true);

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

    // Close menu when clicking a menu item
    chatMenuContent.addEventListener('click', (e) => {
        if (e.target.closest('a')) {
            closeAllChatMenus();
        }
        e.stopPropagation();
    });

    return chatMenu;
}

// Delete folder handler
const deleteFolderHandler = withSession(function(folderId) {
    const folder = chatFolders.find(f => f.id === folderId);
    if (!folder) return;

    const confirmMessage = `Are you sure you want to delete the folder "${folder.name}"? All chats in this folder will be moved to the main chat list.`;

    NotificationModal.confirm('Delete Folder', confirmMessage, async () => {
        try {
            const response = await secureFetch(`/api/chat-folders/${folderId}`, {
                method: 'DELETE'
            });

            const result = await response.json();

            if (response.ok) {
                NotificationModal.toast(result.message || 'Folder deleted successfully', 'success');
                loadChatFolders();
                loadConversations(false, false);
            } else {
                NotificationModal.toast(result.error || 'Failed to delete folder', 'error');
            }
        } catch (error) {
            console.error('Error deleting folder:', error);
            NotificationModal.toast('Failed to delete folder', 'error');
        }
    }, null, { type: 'error', confirmText: 'Delete' });
});

// Show move chat modal
function showMoveChatModal(conversationId) {
    currentMovingConversationId = conversationId;
    
    // Populate folders list
    const foldersList = document.getElementById('foldersList');
    if (!foldersList) return;
    
    foldersList.innerHTML = '';
    
    chatFolders.forEach(folder => {
        const folderOption = document.createElement('button');
        folderOption.className = 'list-group-item list-group-item-action d-flex align-items-center';
        folderOption.innerHTML = `
            <div class="folder-color-indicator me-2" style="background-color: ${folder.color}; width: 12px; height: 12px; border-radius: 50%;"></div>
            <span>${escapeHTML(folder.name)}</span>
        `;
        
        folderOption.addEventListener('click', () => moveChatToFolder(conversationId, folder.id));
        foldersList.appendChild(folderOption);
    });
    
    const modal = new bootstrap.Modal(document.getElementById('moveChatModal'));
    modal.show();
}

// Move chat to folder
const moveChatToFolder = withSession(async function(conversationId, folderId) {
    try {
        const response = await secureFetch(`/api/conversations/${conversationId}/move-to-folder`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ folder_id: folderId })
        });
        
        const result = await response.json();
        
        if (response.ok) {
            NotificationModal.toast(result.message || 'Chat moved successfully', 'success');

            // Close modal
            const modal = bootstrap.Modal.getInstance(document.getElementById('moveChatModal'));
            modal.hide();

            if (folderId) {
                // Moving INTO a folder: remove from main sidebar list
                removeConversationElement(conversationId);
                if (typeof loadedConversationIds !== 'undefined') {
                    loadedConversationIds.delete(conversationId);
                }

                // If the active conversation was moved, open folder and highlight it
                if (Number(conversationId) === Number(currentConversationId)) {
                    await openFolderAndHighlight(folderId, conversationId);
                } else {
                    await reloadFoldersPreservingState();
                }
            } else {
                // Removing FROM a folder: refresh folders and reload main conversation list
                await reloadFoldersPreservingState();
                allConversationsLoaded = false;
                oldestLoadedActivity = null;
                oldestLoadedId = null;
                document.getElementById('dynamic-chats-container').innerHTML = '';
                loadedConversationIds.clear();
                loadConversations(false, true);
            }
        } else {
            NotificationModal.toast(result.error || 'Failed to move chat', 'error');
        }
    } catch (error) {
        console.error('Error moving chat:', error);
        NotificationModal.toast('Failed to move chat', 'error');
    }
});

// Open a folder and highlight a specific conversation inside it
async function openFolderAndHighlight(folderId, conversationId) {
    // Wait for folder section DOM to be rebuilt
    await loadChatFolders();

    // Collapse all folders first
    document.querySelectorAll('.folder-chats-container').forEach(fc => {
        fc.style.display = 'none';
        const id = fc.id.replace('folder-chats-', '');
        if (id) folderPaginationState.delete(Number(id));
    });
    document.querySelectorAll('.folder-item').forEach(item => {
        item.classList.remove('folder-selected');
        item.dataset.folderExpanded = 'false';
        const icon = item.querySelector('.folder-icon');
        if (icon) {
            icon.classList.remove('fa-folder-open');
            icon.classList.add('fa-folder');
        }
    });

    // Expand the target folder
    const container = document.getElementById(`folder-chats-${folderId}`);
    const folderItem = document.querySelector(`[data-folder-id="${folderId}"]`);
    if (!container || !folderItem) return;

    container.style.display = 'block';
    currentSelectedFolderId = folderId;
    folderItem.classList.add('folder-selected');
    folderItem.dataset.folderExpanded = 'true';
    const icon = folderItem.querySelector('.folder-icon');
    if (icon) {
        icon.classList.remove('fa-folder');
        icon.classList.add('fa-folder-open');
    }

    // Load folder chats and highlight the moved conversation via onComplete callback
    await loadFolderChats(folderId, () => {
        const chatEl = container.querySelector(`[data-conversation-id="${conversationId}"]`);
        if (chatEl && Number(conversationId) === Number(currentConversationId)) {
            chatEl.classList.add('active-chat');
        }
    });

    updateNewChatButtonState();
}

// Reload folders while preserving the currently expanded folder
async function reloadFoldersPreservingState() {
    const expandedFolderId = currentSelectedFolderId;
    await loadChatFolders();
    if (expandedFolderId) {
        toggleFolderChats(expandedFolderId);
    }
}

// Remove from folder handler
function removeFromFolderHandler() {
    if (!currentMovingConversationId) return;
    moveChatToFolder(currentMovingConversationId, null);
}

// Setup drag and drop functionality
function setupDragAndDrop() {
    // Make chat items draggable
    makeChatItemsDraggable();
    
    // Make folders drop targets
    makeFoldersDroppable();
}

// Make chat items draggable
function makeChatItemsDraggable() {
    // Function to add drag attributes to existing chat items
    const addDragToChats = () => {
        const chatItems = document.querySelectorAll('[data-conversation-id]:not(.folder-chat-item)');
        chatItems.forEach(chatItem => {
            if (!chatItem.hasAttribute('draggable')) {
                chatItem.setAttribute('draggable', 'true');
                chatItem.addEventListener('dragstart', handleDragStart);
                chatItem.addEventListener('dragend', handleDragEnd);
                
                // Set draggable cursor but don't add visual drag handle
                chatItem.style.cursor = 'grab';
            }
        });
        
        // Enhance existing chat menus with folder options
        chatItems.forEach(chatItem => {
            if (!chatItem.hasAttribute('data-folder-menu-enhanced')) {
                chatItem.setAttribute('data-folder-menu-enhanced', 'true');
                enhanceExistingChatMenu(chatItem);
            }
        });
    };
    
    // Initial setup
    addDragToChats();
    
    // Setup observer for new chat items
    const observer = new MutationObserver((mutations) => {
        let shouldUpdate = false;
        mutations.forEach(mutation => {
            if (mutation.type === 'childList' && mutation.addedNodes.length > 0) {
                shouldUpdate = true;
            }
        });
        
        if (shouldUpdate) {
            // Wait a bit for the original menu to be created
            setTimeout(() => {
                addDragToChats();
            }, 200);
        }
    });
    
    const chatContainer = document.getElementById('dynamic-chats-container');
    if (chatContainer) {
        observer.observe(chatContainer, { childList: true, subtree: true });
    }
}

// Make folders drop targets
function makeFoldersDroppable() {
    // Setup drop zones when folders are rendered
    const setupDropZones = () => {
        const folderItems = document.querySelectorAll('.folder-item');
        folderItems.forEach(folderItem => {
            folderItem.addEventListener('dragover', handleDragOver);
            folderItem.addEventListener('drop', handleDrop);
            folderItem.addEventListener('dragenter', handleDragEnter);
            folderItem.addEventListener('dragleave', handleDragLeave);
        });

        // Make expanded folder chat lists also drop targets (drop onto the open chat list)
        const folderChatsContainers = document.querySelectorAll('.folder-chats-container');
        folderChatsContainers.forEach(container => {
            const folderId = parseInt(container.id.replace('folder-chats-', ''));
            container.addEventListener('dragover', handleDragOver);
            container.addEventListener('drop', (e) => handleDrop(e, folderId));
            container.addEventListener('dragenter', (e) => {
                if (draggedConversationId) {
                    e.preventDefault();
                    // Highlight the parent folder item
                    const folderItem = document.querySelector(`[data-folder-id="${folderId}"]`);
                    if (folderItem) folderItem.classList.add('drag-over');
                }
            });
            container.addEventListener('dragleave', (e) => {
                if (!container.contains(e.relatedTarget)) {
                    const folderItem = document.querySelector(`[data-folder-id="${folderId}"]`);
                    if (folderItem) folderItem.classList.remove('drag-over');
                }
            });
        });
        
        // Make the main chats container a drop zone (to remove from folders)
        const mainChatsContainer = document.getElementById('dynamic-chats-container');
        if (mainChatsContainer && !mainChatsContainer.hasAttribute('data-drop-zone-ready')) {
            mainChatsContainer.setAttribute('data-drop-zone-ready', 'true');
            mainChatsContainer.addEventListener('dragover', handleDragOver);
            mainChatsContainer.addEventListener('drop', (e) => handleDrop(e, null));
            mainChatsContainer.addEventListener('dragenter', (e) => {
                if (draggedConversationId) {
                    e.preventDefault();
                    mainChatsContainer.classList.add('drag-over');
                }
            });
            mainChatsContainer.addEventListener('dragleave', (e) => {
                if (!mainChatsContainer.contains(e.relatedTarget)) {
                    mainChatsContainer.classList.remove('drag-over');
                }
            });
        }

        // Also the load-more button area as drop target
        const loadMoreBtn = document.getElementById('load-more-button');
        if (loadMoreBtn && !loadMoreBtn.hasAttribute('data-drop-zone-ready')) {
            loadMoreBtn.setAttribute('data-drop-zone-ready', 'true');
            loadMoreBtn.addEventListener('dragover', handleDragOver);
            loadMoreBtn.addEventListener('drop', (e) => handleDrop(e, null));
        }
    };
    
    // Setup initially and after folder updates
    setupDropZones();
    
    // Re-setup when folders are updated
    const originalRenderChatFolders = renderChatFolders;
    renderChatFolders = function() {
        originalRenderChatFolders.call(this);
        setupDropZones();
    };
}

// Drag and drop event handlers
let draggedConversationId = null;

function handleDragStart(e) {
    draggedConversationId = e.target.dataset.conversationId;
    e.target.style.opacity = '0.5';
    e.target.style.cursor = 'grabbing';
    
    // Show drop zones
    document.querySelectorAll('.folder-item').forEach(folder => {
        folder.classList.add('drop-zone-active');
    });
    
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/html', e.target.outerHTML);
}

function handleDragEnd(e) {
    e.target.style.opacity = '';
    e.target.style.cursor = 'grab';
    draggedConversationId = null;
    
    // Hide drop zones
    document.querySelectorAll('.folder-item').forEach(folder => {
        folder.classList.remove('drop-zone-active', 'drag-over');
    });
    
    // Remove drag-over highlight from main chats container
    const mainChatsContainer = document.getElementById('dynamic-chats-container');
    if (mainChatsContainer) mainChatsContainer.classList.remove('drag-over');
}

function handleDragOver(e) {
    if (draggedConversationId) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
    }
}

function handleDragEnter(e) {
    if (draggedConversationId) {
        e.preventDefault();
        e.target.closest('.folder-item')?.classList.add('drag-over');
    }
}

function handleDragLeave(e) {
    const folderItem = e.target.closest('.folder-item');
    if (folderItem && !folderItem.contains(e.relatedTarget)) {
        folderItem.classList.remove('drag-over');
    }
}

function handleDrop(e, targetFolderId = null) {
    e.preventDefault();
    
    if (!draggedConversationId) return;
    
    // Get target folder ID if not provided
    if (targetFolderId === null) {
        const folderItem = e.target.closest('.folder-item');
        if (folderItem) {
            targetFolderId = parseInt(folderItem.dataset.folderId);
        }
        // If targetFolderId is still null, it means we're dropping on main area (remove from folder)
    }
    
    // Move the conversation
    moveChatToFolderDragDrop(draggedConversationId, targetFolderId);
    
    // Clean up UI
    document.querySelectorAll('.folder-item').forEach(folder => {
        folder.classList.remove('drag-over');
    });
}

// Move chat to folder via drag and drop
const moveChatToFolderDragDrop = withSession(async function(conversationId, folderId) {
    try {
        const response = await secureFetch(`/api/conversations/${conversationId}/move-to-folder`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ folder_id: folderId })
        });
        
        const result = await response.json();
        
        if (response.ok) {
            NotificationModal.toast(
                folderId ? 'Chat moved to folder successfully' : 'Chat removed from folder successfully',
                'success'
            );

            if (folderId) {
                // Moving INTO a folder: remove from main sidebar list
                removeConversationElement(conversationId);
                if (typeof loadedConversationIds !== 'undefined') {
                    loadedConversationIds.delete(conversationId);
                }

                // If the active conversation was moved, open folder and highlight it
                if (Number(conversationId) === Number(currentConversationId)) {
                    await openFolderAndHighlight(folderId, conversationId);
                } else {
                    // Non-active conversation: open the target folder to show where it landed
                    await openFolderAndHighlight(folderId, conversationId);
                }
            } else {
                // Removing FROM a folder: refresh folders and reload main conversation list
                await reloadFoldersPreservingState();
                allConversationsLoaded = false;
                oldestLoadedActivity = null;
                oldestLoadedId = null;
                document.getElementById('dynamic-chats-container').innerHTML = '';
                loadedConversationIds.clear();
                loadConversations(false, true);
            }
        } else {
            NotificationModal.toast(result.error || 'Failed to move chat', 'error');
        }
    } catch (error) {
        console.error('Error moving chat:', error);
        NotificationModal.toast('Failed to move chat', 'error');
    }
});

// Enhance existing chat menu with folder options
function enhanceExistingChatMenu(chatItem) {
    const conversationId = chatItem.dataset.conversationId;
    if (!conversationId) return;
    
    // Function to add folder options
    const addFolderOptions = () => {
        const chatMenu = chatItem.querySelector('.chat-menu');
        const chatMenuContent = chatMenu?.querySelector('.chat-menu-content');
        
        if (chatMenuContent && !chatMenuContent.querySelector('.folders-menu-section')) {
            // Add folder options to existing menu
            addFolderOptionsToMenu(chatMenuContent, conversationId);
            return true;
        }
        return false;
    };
    
    // Try immediately first
    if (!addFolderOptions()) {
        // Wait for the chat menu to be created by the original script
        setTimeout(() => {
            addFolderOptions();
        }, 100);
    }
}

// Check if a conversation is currently in a folder
function isConversationInFolder(conversationId) {
    // Simple and direct: check if the conversation exists as a folder-chat-item
    const folderChatItem = document.querySelector(`.folder-chat-item[data-conversation-id="${conversationId}"]`);
    const isInFolder = !!folderChatItem;
    
    return isInFolder;
}

// Add folder options to existing chat menu
function addFolderOptionsToMenu(menuContent, conversationId, isInFolderContext = false) {
    // Create folder section
    const folderSection = document.createElement('div');
    folderSection.className = 'folders-menu-section';
    
    // Add separator before folder options
    const separator = document.createElement('div');
    separator.classList.add('menu-separator');
    folderSection.appendChild(separator);
    
    // Add "Move to Folder" submenu
    if (chatFolders.length > 0) {
        const moveToFolderLink = createFolderMenuLink('fa-folder-open', 'Move to Folder', () => {
            showMoveChatModal(conversationId);
        });
        folderSection.appendChild(moveToFolderLink);
    }
    
    // Add "Remove from Folder" option only if chat is in a folder
    const isInFolder = isInFolderContext || isConversationInFolder(conversationId);
    if (isInFolder) {
        const removeFromFolderLink = createFolderMenuLink('fa-times', 'Remove from Folder', () => {
            moveChatToFolderDragDrop(conversationId, null);
        });
        folderSection.appendChild(removeFromFolderLink);
    }
    
    // Try to insert before WhatsApp section, or at the end
    const existingLinks = menuContent.querySelectorAll('.menu-link');
    let insertBeforeElement = null;
    
    // Look for WhatsApp or other platform links
    for (let link of existingLinks) {
        if (link.textContent.toLowerCase().includes('whatsapp') || 
            link.textContent.toLowerCase().includes('telegram')) {
            // Find the separator before this link
            let prevElement = link.previousElementSibling;
            while (prevElement && !prevElement.classList.contains('menu-separator')) {
                prevElement = prevElement.previousElementSibling;
            }
            if (prevElement) {
                insertBeforeElement = prevElement;
                break;
            }
        }
    }
    
    if (insertBeforeElement) {
        menuContent.insertBefore(folderSection, insertBeforeElement);
    } else {
        menuContent.appendChild(folderSection);
    }
}

// Create a menu link similar to the original createMenuLink function
function createFolderMenuLink(iconClass, text, onClick, additionalClass = '') {
    const link = document.createElement('a');
    link.href = '#';
    link.classList.add('menu-link');
    if (additionalClass) {
        link.classList.add(additionalClass);
    }
    
    const icon = document.createElement('i');
    icon.classList.add('fas', iconClass);
    
    const textNode = document.createTextNode(` ${text}`);
    
    link.appendChild(icon);
    link.appendChild(textNode);
    
    link.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        onClick();
        
        // Close the menu after clicking
        const menuContent = link.closest('.chat-menu-content');
        if (menuContent) {
            menuContent.style.display = 'none';
        }
    });
    
    return link;
}

// Update all existing chat menus with current folder options
function updateAllChatMenus() {
    const chatItems = document.querySelectorAll('[data-conversation-id]:not(.folder-chat-item)');
    chatItems.forEach(chatItem => {
        const menuContent = chatItem.querySelector('.chat-menu-content');
        if (menuContent) {
            // Remove existing folder section
            const existingSection = menuContent.querySelector('.folders-menu-section');
            if (existingSection) {
                existingSection.remove();
            }
            
            // Re-add folder options with updated data
            const conversationId = chatItem.dataset.conversationId;
            if (conversationId) {
                addFolderOptionsToMenu(menuContent, conversationId);
            }
        }
    });
}

// Initialize when everything is loaded
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeFoldersDelayed);
} else {
    initializeFoldersDelayed();
}

function initializeFoldersDelayed() {
    // Wait for chat.js to be fully loaded and initialized
    setTimeout(async () => {
        await initializeFolders();
        // Also enhance any existing chat items that might already be loaded
        updateAllChatMenus();
        
        // Setup new chat button integration
        setupNewChatIntegration();
    }, 500);
}

// Update new chat button state based on selected folder
function updateNewChatButtonState() {
    const newChatBtn = document.getElementById('new-chat-main-btn');
    if (!newChatBtn) return;
    
    if (currentSelectedFolderId) {
        // Update button text to show it will create in folder
        const folder = chatFolders.find(f => f.id === currentSelectedFolderId);
        if (folder) {
            const originalText = newChatBtn.getAttribute('data-original-text') || newChatBtn.textContent;
            if (!newChatBtn.getAttribute('data-original-text')) {
                newChatBtn.setAttribute('data-original-text', originalText);
            }
            newChatBtn.innerHTML = `<i class="fas fa-comment-alt"></i> New Chat in "${folder.name}"`;
            newChatBtn.classList.add('btn-folder-selected');
        }
    } else {
        // Restore original button text
        const originalText = newChatBtn.getAttribute('data-original-text');
        if (originalText) {
            newChatBtn.textContent = originalText;
        }
        newChatBtn.classList.remove('btn-folder-selected');
    }
}

// Setup integration with new chat functionality
function setupNewChatIntegration() {
    // Store reference to original startNewConversation function if it exists
    if (typeof window.startNewConversation === 'function') {
        window.originalStartNewConversation = window.startNewConversation;
    }

    // Store reference to original updateActiveChatName function
    if (typeof window.updateActiveChatName === 'function') {
        window.originalUpdateActiveChatName = window.updateActiveChatName;
    }
    
    // Override updateActiveChatName to also update folder chats
    window.updateActiveChatName = function(newName) {
        // Call original function first
        if (typeof window.originalUpdateActiveChatName === 'function') {
            window.originalUpdateActiveChatName(newName);
        }
        
        // Also update in folder chats if applicable
        const folderChatsContainers = document.querySelectorAll('.folder-chats-container');
        folderChatsContainers.forEach(container => {
            const activeChatElement = container.querySelector('.folder-chat-item.active-chat');
            if (activeChatElement) {
                const chatNameSpan = activeChatElement.querySelector('.chat-name');
                if (chatNameSpan) {
                    chatNameSpan.textContent = newName;
                }
            }
        });
        
        // Update folder conversation counts since chat names might affect counts
        loadChatFolders();
    };
    
    // Override startNewConversation to support folders
    window.startNewConversation = function(promptId = null) {
        
        // If no folder selected, use original function
        if (!currentSelectedFolderId && typeof window.originalStartNewConversation === 'function') {
            return window.originalStartNewConversation(promptId);
        }
        
        // Admin check
        if (admin_view) {
            return Promise.resolve();
        }

        if (!isFirstCall && isCurrentConversationEmpty) {
            return Promise.resolve();
        }
        
        let body = {};
        if (promptId !== null) {
            body.prompt_id = promptId;
        }
        
        // Add folder_id if a folder is selected
        if (currentSelectedFolderId) {
            body.folder_id = currentSelectedFolderId;
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
            
            if (currentSelectedFolderId) {
                // If created in folder, reload folder chats and then activate the new chat
                loadFolderChats(currentSelectedFolderId, () => {
                    // Callback executed after folder chats are loaded
                    const newFolderChat = document.querySelector(`.folder-chat-item[data-conversation-id="${data.id}"]`);
                    
                    if (newFolderChat) {
                        // Remove active from all other chats
                        document.querySelectorAll('.active-chat').forEach(el => {
                            el.classList.remove('active-chat');
                        });
                        
                        // Mark this chat as active
                        newFolderChat.classList.add('active-chat');
                        window.selectedChat = newFolderChat;
                    }
                });

                // Update only the conversation count for this specific folder
                updateFolderConversationCount(currentSelectedFolderId);
                
                // Don't add to main conversation list - it's in a folder
            } else {
                // This shouldn't happen since we call original function above
                addConversationElement(data, data.name, null, true);
            }
            
            return continueConversation(data.id, data.name, data.machine);
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
            console.error('Error creating conversation:', error);
            // Fall back to original function if available
            if (typeof window.originalStartNewConversation === 'function') {
                return window.originalStartNewConversation(promptId);
            }
        });
    };
}

// Function to deselect current folder (useful for other parts of the UI)
function deselectCurrentFolder() {
    currentSelectedFolderId = null;
    
    // Collapse all folders
    document.querySelectorAll('.folder-chats-container').forEach(container => {
        container.style.display = 'none';
    });
    
    // Remove active styling
    document.querySelectorAll('.folder-item').forEach(item => {
        item.classList.remove('folder-selected');
    });
    
    updateNewChatButtonState();
}