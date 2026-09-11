/**
 * API Credentials Manager
 * Manages user API keys for AI providers with multiple storage modes
 */

const apiCredentialText = (key, params = {}) => window.AurvekI18n.t(`profile.${key}`, params);

function credentialError(message) {
    const error = new Error(message);
    error.uiSafe = true;
    return error;
}

function credentialErrorMessage(error, fallbackKey) {
    return error?.uiSafe ? error.message : apiCredentialText(fallbackKey);
}

function apiProviderLabel(provider) {
    return {
        openai: 'OpenAI', anthropic: 'Anthropic', google: 'Google AI', xai: 'xAI',
        minimax: 'MiniMax', kimi: 'Kimi', elevenlabs: 'ElevenLabs'
    }[provider] || '';
}

class UserCredentialsManager {
    constructor() {
        this.STORAGE_KEY = 'aurvek_user_api_keys';
        this.STORAGE_MODE_KEY = 'aurvek_credentials_storage_mode';
        this.storageMode = 'session'; // 'session' | 'persistent' | 'server'
        this.storageModes = ['session', 'persistent', 'server'];
        this.providers = ['openai', 'anthropic', 'google', 'xai', 'minimax', 'kimi', 'elevenlabs'];
        this.init();
    }

    /**
     * Initialize the credentials manager
     */
    init() {
        // Load storage mode preference
        const savedMode = localStorage.getItem(this.STORAGE_MODE_KEY);
        if (this.storageModes.includes(savedMode)) {
            this.storageMode = savedMode;
        } else {
            // Legacy fallback: only infer the mode from stored data when there is
            // no explicit preference.  A stale browser copy must never override
            // a successfully saved server/session preference.
            const persistentData = localStorage.getItem(this.STORAGE_KEY);
            if (persistentData) {
                try {
                    const data = JSON.parse(persistentData);
                    if (this.storageModes.includes(data.storageMode)) {
                        this.storageMode = data.storageMode;
                    }
                } catch (error) {
                    console.error('Error parsing stored API credentials:', error);
                }
            }
        }

        // Load existing keys into form
        this.loadKeysToForm().catch(error => {
            console.error('Error loading API credentials:', error);
        });
    }

    isLocalMode(mode) {
        return mode === 'session' || mode === 'persistent';
    }

    getStorageForMode(mode) {
        if (mode === 'session') return sessionStorage;
        if (mode === 'persistent') return localStorage;
        throw credentialError(apiCredentialText('api.error.browser_storage_mode'));
    }

    /**
     * Server responses intentionally contain display masks, never secrets.
     * Keep this check at every write/test boundary so legacy masks cannot be
     * uploaded or tested if they have already leaked into browser storage.
     */
    isMaskedKey(key) {
        if (typeof key !== 'string') return false;
        const value = key.trim();
        return value === '****' || /^[\s\S]{8}\.\.\.[\s\S]{4}$/.test(value);
    }

    getMaskedProviders(keys) {
        if (!keys || typeof keys !== 'object') return [];
        return Object.entries(keys)
            .filter(([, key]) => this.isMaskedKey(key))
            .map(([provider]) => provider);
    }

    getLocalDataForMode(mode) {
        const storage = this.getStorageForMode(mode);
        const stored = storage.getItem(this.STORAGE_KEY);
        if (!stored) return { storageMode: mode, keys: {} };

        let data;
        try {
            data = JSON.parse(stored);
        } catch (error) {
            throw credentialError(apiCredentialText('api.error.invalid_browser_data'));
        }

        const keys = data && typeof data.keys === 'object' && !Array.isArray(data.keys)
            ? { ...data.keys }
            : {};
        return { storageMode: mode, keys };
    }

    saveLocalKeysForMode(mode, keys) {
        const maskedProviders = this.getMaskedProviders(keys);
        if (maskedProviders.length > 0) {
            throw credentialError(apiCredentialText('api.error.masked_cannot_store'));
        }
        this.getStorageForMode(mode).setItem(
            this.STORAGE_KEY,
            JSON.stringify({ storageMode: mode, keys: { ...keys } })
        );
    }

    captureStorageState() {
        return {
            preference: localStorage.getItem(this.STORAGE_MODE_KEY),
            persistent: localStorage.getItem(this.STORAGE_KEY),
            session: sessionStorage.getItem(this.STORAGE_KEY)
        };
    }

    restoreStorageState(snapshot) {
        const restoreItem = (storage, key, value) => {
            if (value === null) storage.removeItem(key);
            else storage.setItem(key, value);
        };

        try {
            restoreItem(localStorage, this.STORAGE_MODE_KEY, snapshot.preference);
            restoreItem(localStorage, this.STORAGE_KEY, snapshot.persistent);
            restoreItem(sessionStorage, this.STORAGE_KEY, snapshot.session);
        } catch (error) {
            console.error('Could not fully restore API credential storage state:', error);
        }
    }

    collectReenteredKeys(requiredKeys) {
        const requiredProviders = Object.keys(requiredKeys || {}).filter(provider => requiredKeys[provider]);
        const providers = Array.from(new Set([...this.providers, ...requiredProviders]));
        const keys = {};
        const missing = [];

        for (const provider of providers) {
            const input = document.getElementById(`key-${provider}`);
            const value = input?.value?.trim() || '';
            const isDisplayMask = input?.dataset?.hasServerKey === 'true' || this.isMaskedKey(value);

            if (value && !isDisplayMask) {
                keys[provider] = value;
            } else if (requiredProviders.includes(provider)) {
                missing.push(provider);
            }
        }

        return { keys, missing };
    }

    /**
     * Get the appropriate storage based on mode
     * @returns {Storage} localStorage or sessionStorage
     */
    getStorage() {
        return this.getStorageForMode(this.storageMode);
    }

    /**
     * Set the storage mode
     * @param {string} mode - 'session' | 'persistent' | 'server'
     */
    async setStorageMode(mode) {
        if (!this.storageModes.includes(mode)) {
            return { success: false, message: apiCredentialText('api.error.invalid_storage_mode') };
        }

        const oldMode = this.storageMode;
        if (oldMode === mode) {
            return { success: true, mode };
        }

        let snapshot = null;

        try {
            snapshot = this.captureStorageState();
            if (this.isLocalMode(oldMode)) {
                // Read the source explicitly before changing this.storageMode.
                const sourceData = this.getLocalDataForMode(oldMode);
                const maskedProviders = this.getMaskedProviders(sourceData.keys);
                if (maskedProviders.length > 0) {
                    const requiredReentry = Object.fromEntries(
                        maskedProviders.map(provider => [provider, true])
                    );
                    const reentry = this.collectReenteredKeys(requiredReentry);
                    if (reentry.missing.length > 0) {
                        const error = credentialError(
                            apiCredentialText('api.error.reenter_before_change', {
                                providers: reentry.missing.map(apiProviderLabel).filter(Boolean).join(', ')
                            })
                        );
                        error.reentryRequired = true;
                        error.missingProviders = reentry.missing;
                        error.maskedProviders = maskedProviders;
                        throw error;
                    }
                    for (const provider of maskedProviders) {
                        sourceData.keys[provider] = reentry.keys[provider];
                    }
                }

                if (mode === 'server') {
                    if (Object.keys(sourceData.keys).length > 0) {
                        const result = await this.saveAllToServer(sourceData.keys);
                        if (!result?.success) {
                            throw credentialError(result?.message || apiCredentialText('api.error.server_save'));
                        }
                    }
                    this.getStorageForMode(oldMode).removeItem(this.STORAGE_KEY);
                } else {
                    this.saveLocalKeysForMode(mode, sourceData.keys);
                    this.getStorageForMode(oldMode).removeItem(this.STORAGE_KEY);
                }
            } else {
                // The server only exposes masks. Read them as metadata so every
                // server-stored provider must be re-entered before going local.
                const serverResult = await this.getAllFromServerResult();
                if (!serverResult.success) {
                    throw credentialError(serverResult.message || apiCredentialText('api.error.server_read'));
                }

                const reentry = this.collectReenteredKeys(serverResult.keys);
                if (reentry.missing.length > 0) {
                    const error = credentialError(
                        apiCredentialText('api.error.reenter_before_browser', {
                            providers: reentry.missing.map(apiProviderLabel).filter(Boolean).join(', ')
                        })
                    );
                    error.reentryRequired = true;
                    error.missingProviders = reentry.missing;
                    throw error;
                }
                this.saveLocalKeysForMode(mode, reentry.keys);
            }

            // Persist the preference and in-memory mode only after migration has
            // completed successfully.
            localStorage.setItem(this.STORAGE_MODE_KEY, mode);
            this.storageMode = mode;
            return { success: true, mode };
        } catch (error) {
            if (snapshot) this.restoreStorageState(snapshot);
            this.storageMode = oldMode;
            return {
                success: false,
                message: credentialErrorMessage(error, 'api.error.storage_change'),
                reentryRequired: Boolean(error.reentryRequired),
                missingProviders: error.missingProviders || [],
                maskedProviders: error.maskedProviders || []
            };
        }
    }

    /**
     * Get all local data from storage
     * @returns {Object} Storage data object
     */
    getAllLocalData() {
        return this.getLocalDataForMode(this.storageMode);
    }

    /**
     * Get local keys only (not from server)
     * @returns {Object} Keys object
     */
    getLocalKeys() {
        return this.getAllLocalData();
    }

    /**
     * Save keys to local storage
     * @param {Object} keys - Keys object to save
     */
    saveLocalKeys(keys) {
        this.saveLocalKeysForMode(this.storageMode, keys);
    }

    /**
     * Set a key for a provider
     * @param {string} provider - Provider name
     * @param {string} key - API key
     */
    async setKey(provider, key) {
        const normalizedKey = typeof key === 'string' ? key.trim() : '';
        if (normalizedKey && this.isMaskedKey(normalizedKey)) {
            return {
                success: false,
                message: apiCredentialText('api.error.masked_cannot_save')
            };
        }

        if (this.storageMode === 'server') {
            return await this.saveToServer(provider, normalizedKey);
        }

        try {
            const data = this.getAllLocalData();
            if (normalizedKey) {
                data.keys[provider] = normalizedKey;
            } else {
                delete data.keys[provider];
            }
            this.saveLocalKeys(data.keys);
            return { success: true };
        } catch (error) {
            return { success: false, message: credentialErrorMessage(error, 'api.error.key_save') };
        }
    }

    /**
     * Get a key for a provider
     * @param {string} provider - Provider name
     * @returns {Promise<string|null>} The API key or null
     */
    async getKey(provider) {
        if (this.storageMode === 'server') {
            return await this.getFromServer(provider);
        }

        const data = this.getAllLocalData();
        return data.keys[provider] || null;
    }

    /**
     * Get all keys (for sending with requests)
     * @returns {Promise<Object>} Keys object
     */
    async getAllKeys() {
        if (this.storageMode === 'server') {
            // Server keys are resolved server-side. GET only returns masks, which
            // must never be sent back as request credentials.
            return {};
        }
        return this.getAllLocalData().keys;
    }

    /**
     * Test an API key
     * @param {string} provider - Provider name
     * @param {string} key - API key to test
     * @returns {Promise<Object>} Test result
     */
    async testKey(provider, key) {
        const normalizedKey = typeof key === 'string' ? key.trim() : '';
        if (!normalizedKey) {
            return { success: false, message: apiCredentialText('api.error.enter_to_test') };
        }
        if (this.isMaskedKey(normalizedKey)) {
            return {
                success: false,
                message: apiCredentialText('api.error.reenter_before_test'),
                masked: true
            };
        }

        try {
            const response = await fetch('/api/test-api-key', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                credentials: 'include',
                body: JSON.stringify({ provider, key: normalizedKey })
            });
            const result = await response.json();
            if (!response.ok || !result?.success) {
                return {
                    success: false,
                    message: result?.message || apiCredentialText('api.error.test_status', { status: response.status })
                };
            }
            return result;
        } catch (error) {
            return { success: false, message: apiCredentialText('api.error.test_network') };
        }
    }

    /**
     * Save a key to the server (server mode)
     * @param {string} provider - Provider name
     * @param {string} key - API key
     * @returns {Promise<Object>} Save result
     */
    async saveToServer(provider, key) {
        if (key && this.isMaskedKey(key)) {
            return {
                success: false,
                message: apiCredentialText('api.error.masked_cannot_save')
            };
        }

        try {
            const response = await fetch('/api/user-credentials', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                credentials: 'include',
                body: JSON.stringify({ provider, key })
            });
            const result = await response.json();

            // Handle not_allowed error (system_only mode)
            if (response.status === 403 && result.error === 'not_allowed') {
                this.showNotAllowedError();
                return { success: false, message: result.message, notAllowed: true };
            }

            if (!response.ok || !result?.success) {
                return {
                    success: false,
                    message: result?.message || apiCredentialText('api.error.key_save_status', { status: response.status })
                };
            }

            return result;
        } catch (error) {
            return { success: false, message: apiCredentialText('api.error.key_save') };
        }
    }

    /**
     * Show error when user is not allowed to configure keys
     */
    showNotAllowedError() {
        NotificationModal.info(
            apiCredentialText('api.system_only_title'),
            apiCredentialText('api.system_only_message')
        );
    }

    /**
     * Save all keys to server
     * @param {Object} keys - Keys object
     * @returns {Promise<Object>} Save result
     */
    async saveAllToServer(keys) {
        const maskedProviders = this.getMaskedProviders(keys);
        if (maskedProviders.length > 0) {
            return {
                success: false,
                message: apiCredentialText('api.error.masked_cannot_upload'),
                maskedProviders
            };
        }
        if (!keys || Object.keys(keys).length === 0) {
            return { success: true, message: apiCredentialText('api.no_keys_to_migrate') };
        }

        try {
            const response = await fetch('/api/user-credentials/batch', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                credentials: 'include',
                body: JSON.stringify({ keys })
            });
            const result = await response.json();

            // Handle not_allowed error (system_only mode)
            if (response.status === 403 && result.error === 'not_allowed') {
                this.showNotAllowedError();
                return { success: false, message: result.message, notAllowed: true };
            }

            if (!response.ok || !result?.success) {
                return {
                    success: false,
                    message: result?.message || apiCredentialText('api.error.keys_save_status', { status: response.status })
                };
            }

            return result;
        } catch (error) {
            return { success: false, message: apiCredentialText('api.error.keys_save') };
        }
    }

    /**
     * Get a key from the server
     * @param {string} provider - Provider name
     * @returns {Promise<string|null>} The API key or null
     */
    async getFromServer(provider) {
        try {
            const response = await fetch(`/api/user-credentials/${provider}`, {
                credentials: 'include',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });
            const data = await response.json();
            if (!response.ok) {
                console.error('Error getting key status from server:', data?.message || response.status);
                return null;
            }
            return data.exists ? data.key : null;
        } catch (error) {
            console.error('Error getting key from server:', error);
            return null;
        }
    }

    /**
     * Get all keys from server
     * @returns {Promise<Object>} Keys object
     */
    async getAllFromServerResult() {
        try {
            const response = await fetch('/api/user-credentials', {
                credentials: 'include',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });
            const data = await response.json();
            if (!response.ok || data?.success === false) {
                return {
                    success: false,
                    keys: {},
                    message: data?.message || apiCredentialText('api.error.keys_read_status', { status: response.status })
                };
            }
            const keys = data?.keys && typeof data.keys === 'object' && !Array.isArray(data.keys)
                ? data.keys
                : {};
            return { success: true, keys };
        } catch (error) {
            console.error('Error getting keys from server:', error);
            return { success: false, keys: {}, message: apiCredentialText('api.error.server_read') };
        }
    }

    async getAllFromServer() {
        const result = await this.getAllFromServerResult();
        return result.success ? result.keys : {};
    }

    /**
     * Delete a key
     * @param {string} provider - Provider name
     */
    async deleteKey(provider) {
        if (this.storageMode === 'server') {
            try {
                const response = await fetch(`/api/user-credentials/${provider}`, {
                    method: 'DELETE',
                    credentials: 'include',
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest'
                    }
                });
                const result = await response.json();
                if (!response.ok || !result?.success) {
                    return {
                        success: false,
                        message: result?.message || apiCredentialText('api.error.key_delete_status', { status: response.status })
                    };
                }
                return result;
            } catch (error) {
                console.error('Error deleting key from server:', error);
                return { success: false, message: apiCredentialText('api.error.key_delete') };
            }
        }

        try {
            const data = this.getAllLocalData();
            delete data.keys[provider];
            this.saveLocalKeys(data.keys);
            return { success: true };
        } catch (error) {
            return { success: false, message: credentialErrorMessage(error, 'api.error.key_delete') };
        }
    }

    /**
     * Clear all keys
     */
    async clearAll() {
        // Clear local storage
        localStorage.removeItem(this.STORAGE_KEY);
        sessionStorage.removeItem(this.STORAGE_KEY);

        // Clear server storage if in server mode
        if (this.storageMode === 'server') {
            try {
                await fetch('/api/user-credentials', {
                    method: 'DELETE',
                    credentials: 'include',
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest'
                    }
                });
            } catch (error) {
                console.error('Error clearing keys from server:', error);
            }
        }
    }

    /**
     * Load keys into the form fields
     */
    async loadKeysToForm() {
        this.syncStorageModeUI();

        let keys;
        if (this.storageMode === 'server') {
            const result = await this.getAllFromServerResult();
            if (!result.success) {
                return result;
            }
            keys = result.keys;
        } else {
            keys = this.getAllLocalData().keys;
        }

        for (const provider of this.providers) {
            const key = keys[provider] || '';
            const input = document.getElementById(`key-${provider}`);
            if (input) {
                input.value = key;
                delete input.dataset.hasServerKey;
                if (key && this.storageMode === 'server') {
                    // This value is display-only and must never be saved/tested.
                    input.dataset.hasServerKey = 'true';
                }
                this.updateStatus(provider, key ? 'saved' : '', key ? apiCredentialText('api.status.saved') : '');
            }
        }
        return { success: true };
    }

    syncStorageModeUI() {
        document.querySelectorAll('input[name="storageMode"]').forEach(radio => {
            radio.checked = radio.value === this.storageMode;
        });
        this.updateStorageInfo();
    }

    /**
     * Update the storage info message
     */
    updateStorageInfo() {
        const infoText = document.getElementById('storageInfoText');
        if (!infoText) return;

        const messages = {
            session: apiCredentialText('api.session_info'),
            persistent: apiCredentialText('api.persistent_info'),
            server: apiCredentialText('api.server_info')
        };

        infoText.textContent = messages[this.storageMode] || messages.session;
    }

    /**
     * Update status indicator for a provider
     * @param {string} provider - Provider name
     * @param {string} status - 'success' | 'error' | 'testing' | 'saved' | ''
     * @param {string} message - Status message
     */
    updateStatus(provider, status, message = '') {
        const statusEl = document.getElementById(`status-${provider}`);
        if (!statusEl) return;

        statusEl.className = 'status-indicator';

        switch (status) {
            case 'success':
                statusEl.innerHTML = '<i class="fas fa-check-circle text-success"></i>';
                statusEl.title = message || apiCredentialText('api.status.valid');
                break;
            case 'error':
                statusEl.innerHTML = '<i class="fas fa-times-circle text-danger"></i>';
                statusEl.title = message || apiCredentialText('api.status.invalid');
                break;
            case 'testing':
                statusEl.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                statusEl.title = apiCredentialText('api.status.testing');
                break;
            case 'saved':
                statusEl.innerHTML = '<i class="fas fa-save text-info"></i>';
                statusEl.title = message || apiCredentialText('api.status.saved');
                break;
            default:
                statusEl.innerHTML = '';
                statusEl.title = '';
        }
    }

    /**
     * Check if user has any configured keys
     * @returns {Promise<boolean>}
     */
    async hasKeys() {
        if (this.storageMode === 'server') {
            const result = await this.getAllFromServerResult();
            return result.success && Object.keys(result.keys).length > 0;
        }
        const keys = await this.getAllKeys();
        return Object.keys(keys).length > 0;
    }

    /**
     * Get keys formatted for API request header
     * @returns {Promise<string|null>} Base64 encoded keys or null
     */
    async getKeysForRequest() {
        if (this.storageMode === 'server') {
            return null;
        }
        const keys = await this.getAllKeys();
        if (Object.keys(keys).length === 0) {
            return null;
        }
        return btoa(JSON.stringify(keys));
    }
}

// Create global instance
window.userCredentials = new UserCredentialsManager();

// DOM Ready - Setup event handlers
document.addEventListener('DOMContentLoaded', () => {
    const manager = window.userCredentials;
    let storageModeChangeInProgress = false;

    // Storage mode change handlers
    document.querySelectorAll('input[name="storageMode"]').forEach(radio => {
        radio.addEventListener('change', async (e) => {
            if (storageModeChangeInProgress) {
                manager.syncStorageModeUI();
                return;
            }

            const radios = Array.from(document.querySelectorAll('input[name="storageMode"]'));
            storageModeChangeInProgress = true;
            radios.forEach(item => { item.disabled = true; });

            try {
                const result = await manager.setStorageMode(e.target.value);
                if (!result.success) {
                    // The manager deliberately keeps its previous mode until all
                    // migration work succeeds. Reflect that rollback in the UI.
                    manager.syncStorageModeUI();
                    NotificationModal.toast(
                        result.message || apiCredentialText('api.error.storage_change'),
                        result.reentryRequired ? 'warning' : 'error'
                    );
                    return;
                }

                const loadResult = await manager.loadKeysToForm();
                if (loadResult?.success === false) {
                    NotificationModal.toast(
                        loadResult.message || apiCredentialText('api.storage_changed_refresh_failed'),
                        'warning'
                    );
                } else {
                    NotificationModal.toast(apiCredentialText('api.storage_changed'), 'info');
                }
            } catch (error) {
                manager.syncStorageModeUI();
                NotificationModal.toast(
                    credentialErrorMessage(error, 'api.error.storage_change'),
                    'error'
                );
            } finally {
                radios.forEach(item => { item.disabled = false; });
                storageModeChangeInProgress = false;
            }
        });
    });

    // Toggle password visibility
    document.querySelectorAll('.toggle-visibility').forEach(btn => {
        btn.addEventListener('click', () => {
            const targetId = btn.dataset.target;
            const input = document.getElementById(targetId);
            const icon = btn.querySelector('i');

            if (input.type === 'password') {
                input.type = 'text';
                icon.classList.remove('fa-eye');
                icon.classList.add('fa-eye-slash');
                btn.setAttribute('title', apiCredentialText('api.hide_key'));
                btn.setAttribute('aria-label', apiCredentialText('api.hide_key'));
            } else {
                input.type = 'password';
                icon.classList.remove('fa-eye-slash');
                icon.classList.add('fa-eye');
                btn.setAttribute('title', apiCredentialText('api.show_key'));
                btn.setAttribute('aria-label', apiCredentialText('api.show_key'));
            }
        });
    });

    // Clear hasServerKey flag when user edits a key input
    manager.providers.forEach(function(provider) {
        var input = document.getElementById('key-' + provider);
        if (input) {
            input.addEventListener('input', function() {
                delete this.dataset.hasServerKey;
            });
        }
    });

    // Test individual key
    document.querySelectorAll('.test-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const provider = btn.dataset.provider;
            const providerLabel = apiProviderLabel(provider);
            const input = document.getElementById(`key-${provider}`);
            const key = input.value.trim();

            if (!key) {
                NotificationModal.toast(apiCredentialText('api.enter_provider_first', { provider: providerLabel }), 'warning');
                return;
            }
            if (input.dataset.hasServerKey === 'true' || manager.isMaskedKey(key)) {
                NotificationModal.toast(
                    apiCredentialText('api.reenter_provider_before_test', { provider: providerLabel }),
                    'warning'
                );
                return;
            }

            manager.updateStatus(provider, 'testing');
            btn.disabled = true;

            const result = await manager.testKey(provider, key);

            btn.disabled = false;

            if (result.success) {
                manager.updateStatus(provider, 'success', apiCredentialText('api.status.valid'));
                NotificationModal.toast(apiCredentialText('api.provider_valid', { provider: providerLabel }), 'success');
            } else {
                manager.updateStatus(provider, 'error', result.message);
                NotificationModal.toast(apiCredentialText('api.provider_invalid', { provider: providerLabel, message: result.message }), 'error');
            }
        });
    });

    // Clear individual key
    document.querySelectorAll('.clear-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const provider = btn.dataset.provider;
            const providerLabel = apiProviderLabel(provider);
            const input = document.getElementById(`key-${provider}`);

            input.value = '';
            await manager.deleteKey(provider);
            manager.updateStatus(provider, '');
            NotificationModal.toast(apiCredentialText('api.provider_cleared', { provider: providerLabel }), 'info');
        });
    });

    // Save all credentials
    document.getElementById('saveAllCredentials')?.addEventListener('click', async () => {
        const btn = document.getElementById('saveAllCredentials');
        btn.disabled = true;
        btn.replaceChildren();
        const savingIcon = document.createElement('i');
        savingIcon.className = 'fas fa-spinner fa-spin';
        btn.append(savingIcon, document.createTextNode(` ${apiCredentialText('status.saving')}`));

        let savedCount = 0;
        let failedCount = 0;
        let maskedCount = 0;

        for (const provider of manager.providers) {
            const input = document.getElementById(`key-${provider}`);
            const key = input?.value.trim();

            if (key && (input.dataset.hasServerKey === 'true' || manager.isMaskedKey(key))) {
                maskedCount++;
                continue;
            }

            if (key) {
                const result = await manager.setKey(provider, key);
                if (result?.success) {
                    manager.updateStatus(provider, 'saved', apiCredentialText('api.status.saved'));
                    savedCount++;
                } else {
                    manager.updateStatus(provider, 'error', result?.message || apiCredentialText('api.error.key_save'));
                    failedCount++;
                }
            }
        }

        btn.disabled = false;
        btn.replaceChildren();
        const saveIcon = document.createElement('i');
        saveIcon.className = 'fas fa-save';
        btn.append(saveIcon, document.createTextNode(` ${apiCredentialText('api.save_all')}`));

        if (failedCount > 0) {
            NotificationModal.toast(
                apiCredentialText('api.save_failed_count', { count: failedCount }),
                'error'
            );
        } else if (savedCount > 0) {
            var credForm = document.getElementById('apiKeysForm');
            if (credForm) FormGuard.markClean(credForm);
            NotificationModal.toast(apiCredentialText('api.saved_count', { count: savedCount }), 'success');
        } else if (maskedCount > 0) {
            NotificationModal.toast(apiCredentialText('api.masks_not_saved'), 'info');
        } else {
            NotificationModal.toast(apiCredentialText('api.no_new_keys'), 'info');
        }
    });

    // Test all credentials
    document.getElementById('testAllCredentials')?.addEventListener('click', async () => {
        const btn = document.getElementById('testAllCredentials');
        btn.disabled = true;
        btn.replaceChildren();
        const testingIcon = document.createElement('i');
        testingIcon.className = 'fas fa-spinner fa-spin';
        btn.append(testingIcon, document.createTextNode(` ${apiCredentialText('api.status.testing')}`));

        let validCount = 0;
        let testedCount = 0;
        let maskedCount = 0;

        for (const provider of manager.providers) {
            const input = document.getElementById(`key-${provider}`);
            const key = input?.value.trim();

            if (key) {
                if (input.dataset.hasServerKey === 'true' || manager.isMaskedKey(key)) {
                    maskedCount++;
                    continue;
                }
                testedCount++;
                manager.updateStatus(provider, 'testing');

                const result = await manager.testKey(provider, key);

                if (result.success) {
                    manager.updateStatus(provider, 'success', apiCredentialText('api.status.valid'));
                    validCount++;
                } else {
                    manager.updateStatus(provider, 'error', result.message);
                }
            }
        }

        btn.disabled = false;
        btn.replaceChildren();
        const testIcon = document.createElement('i');
        testIcon.className = 'fas fa-vial';
        btn.append(testIcon, document.createTextNode(` ${apiCredentialText('api.test_all')}`));

        if (testedCount === 0) {
            NotificationModal.toast(
                maskedCount > 0
                    ? apiCredentialText('api.reenter_before_test_server')
                    : apiCredentialText('api.no_keys_to_test'),
                maskedCount > 0 ? 'warning' : 'info'
            );
        } else {
            NotificationModal.toast(
                maskedCount > 0
                    ? apiCredentialText('api.masked_skipped', {
                        valid: validCount,
                        tested: testedCount,
                        count: maskedCount
                    })
                    : apiCredentialText('api.test_summary', {
                        valid: validCount,
                        tested: testedCount
                    }),
                validCount === testedCount && maskedCount === 0 ? 'success' : 'warning'
            );
        }
    });

    // Clear all credentials
    document.getElementById('clearAllCredentials')?.addEventListener('click', () => {
        NotificationModal.confirm(apiCredentialText('api.clear_all'), apiCredentialText('api.clear_all_confirm'), async () => {
            await manager.clearAll();

            // Clear form inputs
            for (const provider of manager.providers) {
                const input = document.getElementById(`key-${provider}`);
                if (input) {
                    input.value = '';
                    delete input.dataset.hasServerKey;
                }
                manager.updateStatus(provider, '');
            }

            var credForm = document.getElementById('apiKeysForm');
            if (credForm) FormGuard.markClean(credForm);
            NotificationModal.toast(apiCredentialText('api.all_cleared'), 'info');
        }, null, { type: 'error', confirmText: apiCredentialText('api.clear_all') });
    });

    // Initialize tooltips
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl, { html: true });
    });

    // FormGuard -- listener mode for API credentials
    var _fgCredContainer = document.getElementById('apiKeysForm');
    if (_fgCredContainer) {
        FormGuard.watchWithListeners(_fgCredContainer);
    }
});
