/**
 * Unified Notification Modal System
 * Replaces all alert() calls with styled Bootstrap modals
 *
 * Usage:
 *   NotificationModal.success('Title', 'Message');
 *   NotificationModal.error('Title', 'Message');
 *   NotificationModal.warning('Title', 'Message');
 *   NotificationModal.info('Title', 'Message');
 *   NotificationModal.confirm('Title', 'Message', onConfirmCallback, onCancelCallback);
 */

const NotificationModal = {
    _modalElement: null,
    _modalInstance: null,
    _isInitialized: false,

    // A callable label retains its message identity across an embedded locale
    // change. Only text is rebound; dialogs and their form state stay in place.
    _text(element, value) {
        if (typeof value === 'function') AurvekI18n.bindValue(element, value);
        else {
            AurvekI18n.unbind(element);
            element.textContent = value;
        }
    },

    /**
     * Initialize the modal system - creates modal HTML if not exists
     */
    init() {
        if (this._isInitialized) return;

        // Check if modal already exists in DOM
        this._modalElement = document.getElementById('notificationModal');

        if (!this._modalElement) {
            // Create modal HTML dynamically
            const modalHTML = `
                <div class="modal fade" id="notificationModal" tabindex="-1" aria-labelledby="notificationModalLabel" aria-hidden="true">
                    <div class="modal-dialog modal-dialog-centered">
                        <div class="modal-content">
                            <div class="modal-header" id="notificationModalHeader">
                                <div class="d-flex align-items-center">
                                    <span id="notificationModalIcon" class="me-2"></span>
                                    <h5 class="modal-title mb-0" id="notificationModalLabel"></h5>
                                </div>
                                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                            </div>
                            <div class="modal-body" id="notificationModalBody"></div>
                            <div class="modal-footer" id="notificationModalFooter">
                                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal" id="notificationModalCancelBtn"></button>
                                <button type="button" class="btn" id="notificationModalConfirmBtn"></button>
                            </div>
                        </div>
                    </div>
                </div>
            `;
            document.body.insertAdjacentHTML('beforeend', modalHTML);
            this._modalElement = document.getElementById('notificationModal');
        }

        AurvekI18n.bindAttribute(this._modalElement.querySelector('.btn-close'), 'aria-label', 'common.action.close');
        document.getElementById('notificationModalCancelBtn').textContent = AurvekI18n.t('common.action.cancel');
        document.getElementById('notificationModalConfirmBtn').textContent = AurvekI18n.t('common.action.ok');

        // Add styles for modal types
        this._addStyles();

        this._isInitialized = true;
    },

    /**
     * Add CSS styles for different modal types
     * Uses CSS variables from common.css/theme files for theme compatibility
     */
    _addStyles() {
        if (document.getElementById('notification-modal-styles')) return;

        const styles = `
            <style id="notification-modal-styles">
                #notificationModal .modal-content {
                    background-color: var(--bg-secondary);
                    color: var(--text-primary);
                    border: 1px solid var(--border-color);
                    border-radius: 8px;
                    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
                }

                #notificationModal .modal-header {
                    background-color: var(--bg-tertiary);
                    border-bottom: 1px solid var(--border-color);
                    padding: 1rem 1.25rem;
                }

                #notificationModal .modal-title {
                    color: var(--text-primary);
                }

                #notificationModal .modal-body {
                    padding: 1.25rem;
                    font-size: 0.95rem;
                    line-height: 1.5;
                    color: var(--text-secondary);
                }

                #notificationModal .modal-footer {
                    background-color: var(--bg-tertiary);
                    border-top: 1px solid var(--border-color);
                    padding: 0.75rem 1.25rem;
                }

                #notificationModal .btn-close {
                    filter: var(--modal-close-filter, invert(1) grayscale(100%) brightness(200%));
                }

                #notificationModal .btn-secondary {
                    background-color: var(--bg-secondary);
                    border-color: var(--border-color);
                    color: var(--text-primary);
                }

                #notificationModal .btn-secondary:hover {
                    background-color: var(--bg-primary);
                    border-color: var(--border-color);
                }

                #notificationModalIcon {
                    font-size: 1.25rem;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    width: 28px;
                    height: 28px;
                    border-radius: 50%;
                }

                /* Success styling */
                #notificationModal.modal-success #notificationModalHeader {
                    border-left: 4px solid var(--success);
                }
                #notificationModal.modal-success #notificationModalIcon {
                    background-color: var(--success);
                    color: var(--success-btn-text, white);
                }
                #notificationModal.modal-success #notificationModalConfirmBtn {
                    background-color: var(--success);
                    border-color: var(--success);
                    color: var(--success-btn-text, white);
                }
                #notificationModal.modal-success #notificationModalConfirmBtn:hover {
                    filter: brightness(0.9);
                }

                /* Error styling */
                #notificationModal.modal-error #notificationModalHeader {
                    border-left: 4px solid var(--danger);
                }
                #notificationModal.modal-error #notificationModalIcon {
                    background-color: var(--danger);
                    color: white;
                }
                #notificationModal.modal-error #notificationModalConfirmBtn {
                    background-color: var(--danger);
                    border-color: var(--danger);
                    color: white;
                }
                #notificationModal.modal-error #notificationModalConfirmBtn:hover {
                    filter: brightness(0.9);
                }

                /* Warning styling */
                #notificationModal.modal-warning #notificationModalHeader {
                    border-left: 4px solid var(--warning);
                }
                #notificationModal.modal-warning #notificationModalIcon {
                    background-color: var(--warning);
                    color: #000;
                }
                #notificationModal.modal-warning #notificationModalConfirmBtn {
                    background-color: var(--warning);
                    border-color: var(--warning);
                    color: #000;
                }
                #notificationModal.modal-warning #notificationModalConfirmBtn:hover {
                    filter: brightness(0.9);
                }

                /* Info styling */
                #notificationModal.modal-info #notificationModalHeader {
                    border-left: 4px solid var(--info);
                }
                #notificationModal.modal-info #notificationModalIcon {
                    background-color: var(--info);
                    color: var(--info-btn-text, white);
                }
                #notificationModal.modal-info #notificationModalConfirmBtn {
                    background-color: var(--info);
                    border-color: var(--info);
                    color: var(--info-btn-text, white);
                }
                #notificationModal.modal-info #notificationModalConfirmBtn:hover {
                    filter: brightness(0.9);
                }

                /* Confirm styling */
                #notificationModal.modal-confirm #notificationModalHeader {
                    border-left: 4px solid var(--accent);
                }
                #notificationModal.modal-confirm #notificationModalIcon {
                    background-color: var(--accent);
                    color: var(--accent-btn-text, white);
                }
                #notificationModal.modal-confirm #notificationModalConfirmBtn {
                    background-color: var(--accent);
                    border-color: var(--accent);
                    color: var(--accent-btn-text, white);
                }
                #notificationModal.modal-confirm #notificationModalConfirmBtn:hover {
                    filter: brightness(0.9);
                }

                /* Toast container */
                #notificationToastContainer {
                    position: fixed;
                    top: 1rem;
                    right: 1rem;
                    z-index: 9999;
                    display: flex;
                    flex-direction: column;
                    gap: 0.5rem;
                    pointer-events: none;
                    max-width: 380px;
                }

                .notification-toast {
                    display: flex;
                    align-items: center;
                    gap: 0.65rem;
                    padding: 0.75rem 1rem;
                    border-radius: 6px;
                    background-color: var(--bg-secondary);
                    color: var(--text-primary);
                    border: 1px solid var(--border-color);
                    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
                    pointer-events: auto;
                    opacity: 1;
                    transition: opacity 0.3s ease, transform 0.3s ease;
                    font-size: 0.9rem;
                    line-height: 1.4;
                }

                .notification-toast.toast-fade-out {
                    opacity: 0;
                    transform: translateX(20px);
                }

                .notification-toast-icon {
                    flex-shrink: 0;
                    width: 22px;
                    height: 22px;
                    border-radius: 50%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                .notification-toast-icon svg {
                    width: 12px;
                    height: 12px;
                }

                .notification-toast.toast-success { border-left: 4px solid var(--success); }
                .notification-toast.toast-success .notification-toast-icon { background-color: var(--success); color: white; }

                .notification-toast.toast-error { border-left: 4px solid var(--danger); }
                .notification-toast.toast-error .notification-toast-icon { background-color: var(--danger); color: white; }

                .notification-toast.toast-warning { border-left: 4px solid var(--warning); }
                .notification-toast.toast-warning .notification-toast-icon { background-color: var(--warning); color: #000; }

                .notification-toast.toast-info { border-left: 4px solid var(--info); }
                .notification-toast.toast-info .notification-toast-icon { background-color: var(--info); color: white; }

                .notification-toast-message {
                    flex: 1;
                    word-break: break-word;
                }

                .notification-toast-close {
                    flex-shrink: 0;
                    background: none;
                    border: none;
                    color: var(--text-muted);
                    cursor: pointer;
                    font-size: 1.1rem;
                    padding: 0 0.15rem;
                    line-height: 1;
                    opacity: 0.7;
                }
                .notification-toast-close:hover {
                    opacity: 1;
                }
            </style>
        `;
        document.head.insertAdjacentHTML('beforeend', styles);
    },

    /**
     * Get icon SVG based on type
     */
    _getIcon(type) {
        const icons = {
            success: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M13.854 3.646a.5.5 0 0 1 0 .708l-7 7a.5.5 0 0 1-.708 0l-3.5-3.5a.5.5 0 1 1 .708-.708L6.5 10.293l6.646-6.647a.5.5 0 0 1 .708 0z"/></svg>',
            error: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M4.646 4.646a.5.5 0 0 1 .708 0L8 7.293l2.646-2.647a.5.5 0 0 1 .708.708L8.707 8l2.647 2.646a.5.5 0 0 1-.708.708L8 8.707l-2.646 2.647a.5.5 0 0 1-.708-.708L7.293 8 4.646 5.354a.5.5 0 0 1 0-.708z"/></svg>',
            warning: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M8.982 1.566a1.13 1.13 0 0 0-1.96 0L.165 13.233c-.457.778.091 1.767.98 1.767h13.713c.889 0 1.438-.99.98-1.767L8.982 1.566zM8 5c.535 0 .954.462.9.995l-.35 3.507a.552.552 0 0 1-1.1 0L7.1 5.995A.905.905 0 0 1 8 5zm.002 6a1 1 0 1 1 0 2 1 1 0 0 1 0-2z"/></svg>',
            info: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M8 16A8 8 0 1 0 8 0a8 8 0 0 0 0 16zm.93-9.412-1 4.705c-.07.34.029.533.304.533.194 0 .487-.07.686-.246l-.088.416c-.287.346-.92.598-1.465.598-.703 0-1.002-.422-.808-1.319l.738-3.468c.064-.293.006-.399-.287-.47l-.451-.081.082-.381 2.29-.287zM8 5.5a1 1 0 1 1 0-2 1 1 0 0 1 0 2z"/></svg>',
            confirm: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14zm0 1A8 8 0 1 0 8 0a8 8 0 0 0 0 16z"/><path d="M5.255 5.786a.237.237 0 0 0 .241.247h.825c.138 0 .248-.113.266-.25.09-.656.54-1.134 1.342-1.134.686 0 1.314.343 1.314 1.168 0 .635-.374.927-.965 1.371-.673.489-1.206 1.06-1.168 1.987l.003.217a.25.25 0 0 0 .25.246h.811a.25.25 0 0 0 .25-.25v-.105c0-.718.273-.927 1.01-1.486.609-.463 1.244-.977 1.244-2.056 0-1.511-1.276-2.241-2.673-2.241-1.267 0-2.655.59-2.75 2.286zm1.557 5.763c0 .533.425.927 1.01.927.609 0 1.028-.394 1.028-.927 0-.552-.42-.94-1.029-.94-.584 0-1.009.388-1.009.94z"/></svg>'
        };
        return icons[type] || icons.info;
    },

    /**
     * Show a modal with specified type and content
     * @param {string} type - 'success', 'error', 'warning', 'info', 'confirm'
     * @param {string} title - Modal title
     * @param {string} message - Modal message
     * @param {Object} options - Additional options
     */
    show(type, title, message, options = {}) {
        this.init();

        const {
            confirmText = () => AurvekI18n.t('common.action.ok'),
            cancelText = () => AurvekI18n.t('common.action.cancel'),
            showCancel = (type === 'confirm'),
            onConfirm = null,
            onCancel = null,
            hideOnConfirm = true
        } = options;

        // Get elements
        const modalLabel = document.getElementById('notificationModalLabel');
        const modalBody = document.getElementById('notificationModalBody');
        const modalIcon = document.getElementById('notificationModalIcon');
        const confirmBtn = document.getElementById('notificationModalConfirmBtn');
        const cancelBtn = document.getElementById('notificationModalCancelBtn');

        // Remove previous type classes
        this._modalElement.classList.remove('modal-success', 'modal-error', 'modal-warning', 'modal-info', 'modal-confirm');

        // Add current type class
        this._modalElement.classList.add(`modal-${type}`);

        // Set content
        this._text(modalLabel, title);
        if (options.allowHtml === true) {
            AurvekI18n.unbind(modalBody);
            modalBody.innerHTML = message;
        } else {
            this._text(modalBody, message);
        }
        modalIcon.innerHTML = this._getIcon(type);

        // Configure buttons
        confirmBtn.textContent = typeof confirmText === 'function' ? confirmText() : confirmText;
        confirmBtn.style.display = '';
        confirmBtn.disabled = false;

        cancelBtn.textContent = typeof cancelText === 'function' ? cancelText() : cancelText;
        cancelBtn.style.display = showCancel ? '' : 'none';

        // Remove previous event listeners by cloning
        const newConfirmBtn = confirmBtn.cloneNode(true);
        confirmBtn.parentNode.replaceChild(newConfirmBtn, confirmBtn);

        const newCancelBtn = cancelBtn.cloneNode(true);
        cancelBtn.parentNode.replaceChild(newCancelBtn, cancelBtn);
        this._text(newConfirmBtn, confirmText);
        this._text(newCancelBtn, cancelText);

        // Add new event listeners
        document.getElementById('notificationModalConfirmBtn').addEventListener('click', () => {
            if (hideOnConfirm) {
                this.hide();
            }
            if (onConfirm) {
                onConfirm(this);
            }
        });

        document.getElementById('notificationModalCancelBtn').addEventListener('click', () => {
            this.hide();
            if (onCancel) {
                onCancel(this);
            }
        });

        // Show modal
        this._modalInstance = new bootstrap.Modal(this._modalElement);
        this._modalInstance.show();

        return this;
    },

    /**
     * Hide the modal
     */
    hide() {
        if (this._modalInstance) {
            this._modalInstance.hide();
        }
    },

    /**
     * Update modal content (useful for async operations)
     */
    update(options = {}) {
        const { title, message, confirmText, cancelText, showConfirm, showCancel } = options;

        if (title !== undefined) {
            this._text(document.getElementById('notificationModalLabel'), title);
        }
        if (message !== undefined) {
            const modalBody = document.getElementById('notificationModalBody');
            if (options.allowHtml === true) {
                AurvekI18n.unbind(modalBody);
                modalBody.innerHTML = message;
            } else {
                this._text(modalBody, message);
            }
        }
        if (confirmText !== undefined) {
            this._text(document.getElementById('notificationModalConfirmBtn'), confirmText);
        }
        if (cancelText !== undefined) {
            this._text(document.getElementById('notificationModalCancelBtn'), cancelText);
        }
        if (showConfirm !== undefined) {
            document.getElementById('notificationModalConfirmBtn').style.display = showConfirm ? '' : 'none';
        }
        if (showCancel !== undefined) {
            document.getElementById('notificationModalCancelBtn').style.display = showCancel ? '' : 'none';
        }

        return this;
    },

    /**
     * Show success modal
     */
    success(title, message, options = {}) {
        return this.show('success', title, message, options);
    },

    /**
     * Show error modal
     */
    error(title, message, options = {}) {
        return this.show('error', title, message, options);
    },

    /**
     * Show warning modal
     */
    warning(title, message, options = {}) {
        return this.show('warning', title, message, options);
    },

    /**
     * Show info modal
     */
    info(title, message, options = {}) {
        return this.show('info', title, message, options);
    },

    /**
     * Show a toast notification (non-blocking, auto-dismissing)
     * @param {string} message - Toast message
     * @param {string} type - 'success', 'error', 'warning', 'info' (maps 'danger' to 'error')
     * @param {number} duration - Auto-dismiss after ms (default 5000)
     */
    toast(message, type = 'info', duration = 5000) {
        if (type === 'danger') type = 'error';

        // Ensure container exists
        let container = document.getElementById('notificationToastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'notificationToastContainer';
            document.body.appendChild(container);
            // Ensure styles are loaded
            this.init();
        }

        const iconMap = {
            success: '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="currentColor" viewBox="0 0 16 16"><path d="M13.854 3.646a.5.5 0 0 1 0 .708l-7 7a.5.5 0 0 1-.708 0l-3.5-3.5a.5.5 0 1 1 .708-.708L6.5 10.293l6.646-6.647a.5.5 0 0 1 .708 0z"/></svg>',
            error: '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="currentColor" viewBox="0 0 16 16"><path d="M4.646 4.646a.5.5 0 0 1 .708 0L8 7.293l2.646-2.647a.5.5 0 0 1 .708.708L8.707 8l2.647 2.646a.5.5 0 0 1-.708.708L8 8.707l-2.646 2.647a.5.5 0 0 1-.708-.708L7.293 8 4.646 5.354a.5.5 0 0 1 0-.708z"/></svg>',
            warning: '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="currentColor" viewBox="0 0 16 16"><path d="M8.982 1.566a1.13 1.13 0 0 0-1.96 0L.165 13.233c-.457.778.091 1.767.98 1.767h13.713c.889 0 1.438-.99.98-1.767L8.982 1.566zM8 5c.535 0 .954.462.9.995l-.35 3.507a.552.552 0 0 1-1.1 0L7.1 5.995A.905.905 0 0 1 8 5zm.002 6a1 1 0 1 1 0 2 1 1 0 0 1 0-2z"/></svg>',
            info: '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="currentColor" viewBox="0 0 16 16"><path d="M8 16A8 8 0 1 0 8 0a8 8 0 0 0 0 16zm.93-9.412-1 4.705c-.07.34.029.533.304.533.194 0 .487-.07.686-.246l-.088.416c-.287.346-.92.598-1.465.598-.703 0-1.002-.422-.808-1.319l.738-3.468c.064-.293.006-.399-.287-.47l-.451-.081.082-.381 2.29-.287zM8 5.5a1 1 0 1 1 0-2 1 1 0 0 1 0 2z"/></svg>'
        };

        const toast = document.createElement('div');
        toast.className = `notification-toast toast-${type}`;

        const iconSpan = document.createElement('span');
        iconSpan.className = 'notification-toast-icon';
        iconSpan.innerHTML = iconMap[type] || iconMap.info;  // trusted hardcoded SVG

        const messageSpan = document.createElement('span');
        messageSpan.className = 'notification-toast-message';
        this._text(messageSpan, message);  // XSS-safe for caller content

        const closeBtn = document.createElement('button');
        closeBtn.className = 'notification-toast-close';
        AurvekI18n.bindAttribute(closeBtn, 'aria-label', 'common.action.close');
        closeBtn.innerHTML = '&times;';  // trusted hardcoded glyph

        toast.appendChild(iconSpan);
        toast.appendChild(messageSpan);
        toast.appendChild(closeBtn);

        container.appendChild(toast);

        const removeToast = () => {
            toast.classList.add('toast-fade-out');
            setTimeout(() => toast.remove(), 300);
        };

        toast.querySelector('.notification-toast-close').addEventListener('click', removeToast);

        if (duration > 0) {
            setTimeout(removeToast, duration);
        }

        return toast;
    },

    /**
     * Show confirmation modal with two buttons
     */
    confirm(title, message, onConfirm, onCancel = null, options = {}) {
        const type = options.type || 'confirm';
        return this.show(type, title, message, {
            showCancel: true,
            confirmText: options.confirmText || (() => AurvekI18n.t('common.action.confirm')),
            cancelText: options.cancelText || (() => AurvekI18n.t('common.action.cancel')),
            onConfirm,
            onCancel,
            hideOnConfirm: options.hideOnConfirm !== false,
            allowHtml: options.allowHtml === true
        });
    }
};

// Auto-initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => NotificationModal.init());
} else {
    NotificationModal.init();
}

/**
 * Escape HTML to prevent XSS in dynamic content.
 *
 * Moved from chat.js in PR 1 (F3 round 8 fix) so it is available on every
 * page that loads notification-modal.js (base.html:160, base_admin.html:152,
 * chat.html:711) instead of only on the chat page. Top-level global function,
 * NOT a method on NotificationModal, so existing unqualified callers in
 * chat.js and future PR 2 caller migrations in non-chat views keep working
 * without having to qualify the call.
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Export for module systems
if (typeof module !== 'undefined' && module.exports) {
    module.exports = NotificationModal;
}
