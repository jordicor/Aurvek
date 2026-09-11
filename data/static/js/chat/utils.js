/* utils.js */

const Config = {
    isAudioPlaying: false,
    currentAudio: null,
    currentAudioIcon: null,
    currentStopIcon: null,
    attachedFiles: [],
    have_vision: true,
    can_send_files: false,
    attachment_upload_chunk_size_bytes: 2 * 1024 * 1024,
    mediaRecorder: null,
    audioChunks: []
};

function startCountdown(targetElementId, durationInSeconds) {
    let endTime = localStorage.getItem("countdownEndTime");
    let now = new Date().getTime();

    if (!endTime || now > endTime) {
        endTime = now + durationInSeconds * 1000;
        localStorage.setItem("countdownEndTime", endTime);
    }

    let countdownFunction = setInterval(function() {
        now = new Date().getTime();
        let timeleft = endTime - now;
        
        let hours = Math.floor((timeleft % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
        let minutes = Math.floor((timeleft % (1000 * 60 * 60)) / (1000 * 60));
        let seconds = Math.floor((timeleft % (1000 * 60)) / 1000);
        
        document.getElementById(targetElementId).textContent = window.AurvekI18n.t('chat.countdown', { hours, minutes, seconds });
        
        if (timeleft < 0) {
            clearInterval(countdownFunction);
            document.getElementById(targetElementId).textContent = window.AurvekI18n.t('chat.expired');
            localStorage.removeItem("countdownEndTime");
        }
    }, 1000);
}


function escapeHTML(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function encodeForHTML(str) {
    return str.replace(/[\u00A0-\u9999<>\&"']/gim, function(i) {
        return {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        }[i] || '&#' + i.charCodeAt(0) + ';';
    });
}


function removeWaitingMessage() {
    var temporaryMessages = document.querySelectorAll('.temporary-message');
    temporaryMessages.forEach(function(message) {
        message.remove();
    });
}    

// Form submit handler moved to main.js to avoid conflicts
// document.getElementById('form-message').onsubmit = function(e) { ... };



function deleteConversation(conversationId) {
    NotificationModal.confirm(
        window.AurvekI18n.t('chat.delete_confirm_title'),
        window.AurvekI18n.t('chat.delete_confirm'),
        withSession(() => {
            // Close dropdown menu before proceeding
            const conversationElement = document.querySelector(`[data-conversation-id="${conversationId}"]`);
            if (conversationElement) {
                const chatMenu = conversationElement.querySelector('.chat-menu');
                const bootstrapDropdown = bootstrap.Dropdown.getInstance(chatMenu.querySelector('.dropdown-toggle'));
                if (bootstrapDropdown) {
                    bootstrapDropdown.hide();
                }
            }

            // First, remove association with all external platforms
            removeAllExternalPlatformAssociations(conversationId)
                .then(() => {
                    // Then, delete conversation using secureFetch
                    return secureFetch(`/api/conversations/${conversationId}`, {
                        method: 'DELETE'
                    });
                })
                .then(response => {
                    if (!response.ok) {
                        throw new Error('Could not delete the conversation');
                    }
                    return response.json();
                })
                .then(data => {
                    const conversationElement = document.querySelector(`[data-conversation-id="${conversationId}"]`);
                    const wasExternal = conversationElement && conversationElement.closest('#external-chats-container');

                    removeConversationElement(conversationId);

                    if (wasExternal) {
                        updateExternalSection();
                    }

                    if (currentConversationId == conversationId) {
                        deactivateChat();
                        isCurrentConversationEmpty = false;
                    }
                })
                .catch(error => {
                    console.error('Error deleting the chat:', error);
                    NotificationModal.error(window.AurvekI18n.t('chat.delete_error_title'), window.AurvekI18n.t('chat.delete_error'));
                });
        }),
        null,
        { type: 'error', confirmText: window.AurvekI18n.t('chat.delete') }
    );
}

function toggleLockConversation(conversationId, lock) {
    const action = lock ? 'lock' : 'unlock';
    const actionCapitalized = lock ? window.AurvekI18n.t('chat.lock') : window.AurvekI18n.t('chat.unlock');

    NotificationModal.confirm(
        lock ? window.AurvekI18n.t('chat.lock_confirm_title') : window.AurvekI18n.t('chat.unlock_confirm_title'),
        lock ? window.AurvekI18n.t('chat.lock_confirm') : window.AurvekI18n.t('chat.unlock_confirm'),
        withSession(() => {
            secureFetch(`/api/conversations/${conversationId}/lock`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lock: lock })
            })
            .then(response => {
                if (!response.ok) throw new Error(`Could not ${action} the conversation`);
                return response.json();
            })
            .then(data => {
                // Update UI
                const conversationElement = document.querySelector(`[data-conversation-id="${conversationId}"]`);
                if (conversationElement) {
                    conversationElement.dataset.locked = lock ? 'true' : 'false';
                    if (lock) {
                        conversationElement.classList.add('conversation-locked');
                    } else {
                        conversationElement.classList.remove('conversation-locked');
                    }

                    // Update icon in sidebar
                    const nameSpan = conversationElement.querySelector('.chat-name');
                    if (nameSpan) {
                        const chatText = nameSpan.textContent.replace(/^\s*/, '');
                        if (lock) {
                            nameSpan.innerHTML = `<i class="fas fa-comment-slash" title="${escapeHTML(window.AurvekI18n.t('chat.locked'))}"></i> ${escapeHTML(chatText)}`;
                        } else {
                            // Remove lock icon
                            const lockIcon = nameSpan.querySelector('.fa-comment-slash');
                            if (lockIcon) lockIcon.remove();
                        }
                    }
                }

                // Update current conversation state if this is the active one
                if (currentConversationId == conversationId) {
                    isCurrentConversationLocked = lock;
                    const lockedBanner = document.getElementById('locked-conversation-banner');
                    const messageText = document.getElementById('message-text');

                    if (lock) {
                        if (lockedBanner) lockedBanner.style.display = 'flex';
                        if (messageText) {
                            messageText.placeholder = window.AurvekI18n.t('chat.locked');
                            messageText.disabled = true;
                        }
                        document.querySelector('#form-message button[type="submit"]').disabled = true;
                    } else {
                        if (lockedBanner) lockedBanner.style.display = 'none';
                        if (messageText) {
                            messageText.placeholder = window.AurvekI18n.t('chat.type_message');
                            messageText.disabled = false;
                        }
                        document.querySelector('#form-message button[type="submit"]').disabled = false;
                    }
                }

                NotificationModal.success(lock ? window.AurvekI18n.t('chat.lock_success_title') : window.AurvekI18n.t('chat.unlock_success_title'), lock ? window.AurvekI18n.t('chat.lock_success') : window.AurvekI18n.t('chat.unlock_success'));
            })
            .catch(error => {
                console.error(`Error ${action}ing the chat:`, error);
                NotificationModal.error(lock ? window.AurvekI18n.t('chat.lock_error_title') : window.AurvekI18n.t('chat.unlock_error_title'), lock ? window.AurvekI18n.t('chat.lock_error') : window.AurvekI18n.t('chat.unlock_error'));
            });
        }),
        null,
        { type: lock ? 'warning' : 'success', confirmText: actionCapitalized }
    );
}

function removeAllExternalPlatformAssociations(conversationId) {
    return secureFetch(`/api/conversations/${conversationId}/external-platform`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ 
            action: 'remove',
            platform: 'all'
        })
    })
    .then(response => {
        if (!response.ok) {
            throw new Error('Failed to remove external platform associations');
        }
        return response.json();
    });
}

function updateExternalSection() {
    const externalChatsContainer = document.querySelector('#external-chats-container');
    const externalSection = document.querySelector('.external-section');
    
    if (externalChatsContainer.children.length === 0) {
        externalSection.style.display = 'none';
    } else {
        externalSection.style.display = 'block';
    }
}

function downloadPDF(conversationId) {
    if (String(window.applicationConversation?.id) === String(conversationId) || window.AurvekEmbed?.config.application) {
        return window.AurvekChatActions.fromControl('export_pdf');
    }
    NotificationModal.confirm(
        window.AurvekI18n.t('chat.download_pdf'),
        window.AurvekI18n.t('chat.download_pdf_confirm'),
        withSession((modal) => {
            modal.update({ message: window.AurvekI18n.t('chat.processing'), showConfirm: false, showCancel: false });

            secureFetch(`/download-pdf/${conversationId}`)
                .then(response => {
                    if (!response.ok) {
                        throw new Error('Request error');
                    }
                    return response.json();
                })
                .then(data => {
                    modal.update({ title: window.AurvekI18n.t('chat.pdf_started'), message: data.message, showCancel: true, cancelText: window.AurvekI18n.t('chat.close') });
                })
                .catch(error => {
                    console.error('Error:', error);
                    modal.update({ title: window.AurvekI18n.t('chat.error'), message: window.AurvekI18n.t('chat.pdf_start_error'), showCancel: true, cancelText: window.AurvekI18n.t('chat.close') });
                });
        }),
        null,
        { confirmText: window.AurvekI18n.t('chat.download'), hideOnConfirm: false }
    );
}



function downloadAudio(conversationId) {
    if (String(window.applicationConversation?.id) === String(conversationId) || window.AurvekEmbed?.config.application) {
        return window.AurvekChatActions.fromControl('export_mp3');
    }
    NotificationModal.confirm(
        window.AurvekI18n.t('chat.download_mp3'),
        window.AurvekI18n.t('chat.download_mp3_confirm'),
        withSession((modal) => {
            modal.update({ message: window.AurvekI18n.t('chat.processing'), showConfirm: false, showCancel: false });

            secureFetch(`/download-mp3/${conversationId}`)
                .then(response => {
                    if (!response.ok) {
                        throw new Error('Request error');
                    }
                    return response.json();
                })
                .then(data => {
                    modal.update({ title: window.AurvekI18n.t('chat.mp3_started'), message: data.message, showCancel: true, cancelText: window.AurvekI18n.t('chat.close') });
                })
                .catch(error => {
                    console.error('Error:', error);
                    modal.update({ title: window.AurvekI18n.t('chat.error'), message: window.AurvekI18n.t('chat.mp3_start_error'), showCancel: true, cancelText: window.AurvekI18n.t('chat.close') });
                });
        }),
        null,
        { confirmText: window.AurvekI18n.t('chat.download'), hideOnConfirm: false }
    );
}

function serveMp3(conversationId) {
    secureFetch(`/serve-mp3/${conversationId}`, {
        method: 'GET'
    })
        .then(response => {
            if (!response.ok) {
                throw new Error('MP3 not available');
            }
            return response.blob();
        })
        .then(blob => {
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = `conversation_${conversationId}.mp3`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
        })
        .catch(error => {
            console.error('Error:', error);
            NotificationModal.error(window.AurvekI18n.t('chat.mp3_unavailable'), window.AurvekI18n.t('chat.mp3_unavailable_detail'));
        });
}


function toggleSendButton(streaming = false) {
    const sendButton = document.getElementById('send-button');
    sendButton.dataset.streaming = String(streaming);
    if (streaming) {
        sendButton.innerText = window.AurvekI18n.t('chat.stop');
        sendButton.onclick = stopReceivingStream;
    } else {
        sendButton.innerText = window.AurvekI18n.t('chat.send');
        sendButton.onclick = handleSendButtonClick;
    }
    window.AurvekEmbed?.syncSendControl(streaming);
    window.updateIncognitoChatControls?.();
}

function handleSendButtonClick(event) {
    event.preventDefault();
    const messageText = document.getElementById('message-text').value;

    // Check session before sending message (force check for critical action)
    SessionManager.validateSession(true).then((isValid) => {
        if (isValid) {
            // Session is valid, send message and clear form
            const didSend = sendMessage(messageText);
            if (didSend) {
                document.getElementById('message-text').value = '';
                Config.attachedFiles = [];
            }
        } else {
            // Session invalid, modal already shown by validateSession
            // Don't clear the form so user can copy their message
        }
    });
}

function addLoadingIndicator(messageText = '') {
    var chatWindow = document.getElementById('chat-window');
    var loadingIndicator = document.createElement('div');
    loadingIndicator.classList.add('loading-indicator', 'temporary-message');
    loadingIndicator.innerHTML = `
        <div class="spinner-border text-primary" role="status">
            <span class="visually-hidden">${escapeHTML(window.AurvekI18n.t('chat.loading'))}</span>
        </div>
    `;
    window.AurvekI18n.bindText(loadingIndicator.querySelector('.visually-hidden'), 'chat.loading');

    const label = typeof messageText === 'string' ? messageText.trim() : '';
    if (label) {
        loadingIndicator.style.flexDirection = 'column';
        const textEl = document.createElement('div');
        textEl.classList.add('loading-indicator-text');
        textEl.textContent = label;
        loadingIndicator.appendChild(textEl);
    }

    chatWindow.appendChild(loadingIndicator);

    // Ensure loading indicator is completely visible
    loadingIndicator.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

function removeLoadingIndicator() {
    var chatWindow = document.getElementById('chat-window');
    var loadingIndicator = chatWindow.querySelector('.loading-indicator');
    if (loadingIndicator) {
        chatWindow.removeChild(loadingIndicator);
    }
}

function showInsufficientBalancePopup() {
    if (window.AurvekEmbed) {
        NotificationModal.warning(() => window.AurvekI18n.t('chat.balance_title'),
            () => window.AurvekI18n.t('embed.balance'));
        return;
    }
    const message = document.createElement('div');
    message.append(document.createTextNode(window.AurvekI18n.t('chat.balance_message')), document.createElement('br'));
    const link = document.createElement('a');
    link.href = '/';
    link.textContent = window.AurvekI18n.t('chat.balance_reload');
    message.appendChild(link);
    NotificationModal.warning(window.AurvekI18n.t('chat.balance_title'), message.innerHTML, { allowHtml: true });
}

    //console.log("Initializing imageHandler");
    const imageHandler = {
        images: [],
        init: function(images) {
            //console.log("Initializing with images:", images);
            this.images = images;
            this.setupEventListeners();
            this.initializeImages();
        },
        setupEventListeners: function() {
            //console.log("Setting up event listeners");
            const fullsizeContainer = document.getElementById('fullsizeContainer');
            if (fullsizeContainer) {
                fullsizeContainer.addEventListener('click', (event) => {
                    if (event.target === fullsizeContainer) {
                        this.closeFullsize();
                    }
                });
            } else {
                console.error("Element 'fullsizeContainer' not found");
            }
            document.addEventListener('keydown', this.handleEscapeKey.bind(this));
        },
        initializeImages: function() {
            //console.log("Initializing images");
            const images = document.querySelectorAll('#chat-messages-container .message-content img');
            images.forEach(img => {
                if (!img.dataset.initialized) {
                    img.dataset.fullsize = img.dataset.fullsize || img.src.replace('_256.webp', '_fullsize.webp');
                    img.onclick = () => this.showFullsize(img.dataset.fullsize, img.dataset.messageId, img.dataset.attachmentRef);
                    img.dataset.initialized = 'true';
                }
            });
        },
		
		showFullsize: function(url, messageId, attachmentRef) {
			const fullsizeContainer = document.getElementById('fullsizeContainer');
			const fullsizeImage = document.getElementById('fullsizeImage');
			const downloadButton = document.getElementById('downloadButton');
			const deleteButton = document.getElementById('deleteButton');

			if (!fullsizeContainer || !fullsizeImage || !downloadButton || !deleteButton) {
				console.error("One or more required elements not found");
				return;
			}

			// Modify URL to get fullsize version
			const fullsizeUrl = window.AurvekEmbed ? window.AurvekEmbed.resourceUrl(url)
                : url.replace('_256.webp', '_fullsize.webp');
            if (!fullsizeUrl) return;

			// Hide current image
			fullsizeImage.style.display = 'none';

			// Show loading indicator
			const loadingIndicator = document.createElement('div');
			loadingIndicator.className = 'loading-indicator';
			loadingIndicator.innerHTML = `<div class="spinner-border text-light" role="status"><span class="visually-hidden">${escapeHTML(window.AurvekI18n.t('chat.loading'))}</span></div>`;
			fullsizeContainer.appendChild(loadingIndicator);

			// Show container
			fullsizeContainer.style.display = 'block';

			// Change image source
			fullsizeImage.src = fullsizeUrl;

			// Wait for image to fully load
			fullsizeImage.onload = function() {
				// Hide loading indicator
				fullsizeContainer.removeChild(loadingIndicator);

				// Show new image
				fullsizeImage.style.display = 'block';
			};

			fullsizeImage.onerror = function() {
				console.error("Error loading image");
				fullsizeContainer.removeChild(loadingIndicator);
				fullsizeImage.style.display = 'block'; // Show error image if exists
			};

			// Hide or show download/delete buttons as appropriate
			// Only show for message images (with messageId), not for avatar/profile images
			if (!messageId) {
				downloadButton.style.display = 'none';
				deleteButton.style.display = 'none';
			} else {
				downloadButton.style.display = '';
				deleteButton.style.display = window.AurvekEmbed && !attachmentRef ? 'none' : '';
			}

			downloadButton.onclick = (e) => {
				e.preventDefault();
				this.downloadImage(fullsizeUrl);
			};

			deleteButton.onclick = () => {
				if (messageId) {
					this.deleteImage(messageId, attachmentRef);
				} else {
					NotificationModal.warning(window.AurvekI18n.t('chat.cannot_delete'), window.AurvekI18n.t('chat.image_cannot_delete'));
				}
			};
		},

		
        closeFullsize: function() {
            //console.log("Closing fullsize image");
            const fullsizeContainer = document.getElementById('fullsizeContainer');
            if (fullsizeContainer) {
                fullsizeContainer.style.display = 'none';
            } else {
                console.error("Element 'fullsizeContainer' not found");
            }
        },
        handleEscapeKey: function(event) {
            if (event.key === 'Escape') {
                this.closeFullsize();
            }
        },
        downloadImage: function(url) {
            const link = document.createElement('a');
            link.href = url;
            const randomName = Math.random().toString(36).substring(2, 8);
            link.download = `${randomName}.png`;
            link.target = '_blank';
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        },

		deleteImage: function(messageId, attachmentRef) {
            if (window.AurvekEmbed?.config.application) {
                if (attachmentRef) deleteApplicationAttachment(attachmentRef, () => this.closeFullsize());
                return;
            }
			NotificationModal.confirm(
				window.AurvekI18n.t('chat.delete_confirm_title'),
				window.AurvekI18n.t('chat.image_delete_confirm'),
				() => {
					const url = attachmentRef
						? `/api/delete-image/${messageId}?attachment_ref=${encodeURIComponent(attachmentRef)}`
						: `/api/delete-image/${messageId}`;
					fetch(url, {
						method: 'DELETE',
					})
					.then(response => response.json())
					.then(data => {
						if (data.success) {
							this.closeFullsize();
							this.onImagesDeleted([messageId]);
							this.images = this.images.filter(img => img.id !== messageId);

							// Find image element
							const imgElement = document.querySelector(`img[data-message-id="${messageId}"]`);
							if (imgElement) {
								const messageElement = imgElement.closest('.message');
								if (messageElement) {
									// Create correct message structure
									const messageContentContainer = document.createElement('div');
									messageContentContainer.classList.add('message-content-container');

									const messageContent = document.createElement('div');
									messageContent.classList.add('message-content');

									const deletedText = document.createElement('p');
									deletedText.textContent = window.AurvekI18n.t('chat.image_deleted');

									// Build message structure
									messageContent.appendChild(deletedText);
									messageContentContainer.appendChild(messageContent);

									// Replace message content
									messageElement.innerHTML = '';
									messageElement.appendChild(messageContentContainer);
								}
							}
						} else {
							console.error('Error deleting image:', data.error);
						}
					})
					.catch(error => console.error('Error:', error));
				},
				null,
				{ type: 'error', confirmText: window.AurvekI18n.t('chat.delete') }
			);
		},
		

        onImagesDeleted: function(deletedIds) {
            // Images deleted callback
        }
    };
    
    document.addEventListener('DOMContentLoaded', function() {
        imageHandler.init([]);
    });
    
    // Function to initialize newly added images
    function initializeNewImages(container) {
        const images = container.querySelectorAll('.message-content img:not([data-initialized])');
        images.forEach(img => {
            img.dataset.fullsize = img.dataset.fullsize || img.src.replace('_256.webp', '_fullsize.webp');
            img.onclick = () => imageHandler.showFullsize(img.dataset.fullsize, img.dataset.messageId, img.dataset.attachmentRef);
            img.dataset.initialized = 'true';
        });
    }
    
    // Mutation observer to detect new images added to DOM
    const observer = new MutationObserver((mutations) => {
        mutations.forEach((mutation) => {
            if (mutation.type === 'childList') {
                mutation.addedNodes.forEach((node) => {
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        initializeNewImages(node);
                    }
                });
            }
        });
    });
    
    // Configure and start observer
    const config = { childList: true, subtree: true };
    const targetNode = document.getElementById('chat-messages-container');
    observer.observe(targetNode, config);

    

// The scoped attachment route removes only this file and rewrites its message.
function deleteApplicationAttachment(attachmentRef, onDeleted) {
    NotificationModal.confirm(
        window.AurvekI18n.t('chat.delete_confirm_title'),
        window.AurvekI18n.t('chat.delete'),
        async () => {
            try {
                const response = await fetch(`/api/attachments/${encodeURIComponent(attachmentRef)}`, {method: 'DELETE'});
                if (!response.ok) throw new Error('Attachment deletion failed');
                onDeleted?.();
                await refreshActiveConversation();
            } catch (error) {
                NotificationModal.error(window.AurvekI18n.t('chat.delete'), window.AurvekI18n.t('embed.error'));
            }
        }, null, {type: 'error', confirmText: window.AurvekI18n.t('chat.delete')}
    );
}
