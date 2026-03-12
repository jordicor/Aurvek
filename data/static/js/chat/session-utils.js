/* session-utils.js - Centralized Session Management Utilities */

// Session validation utility with activity detection and optimized resource usage
const SessionManager = {
    // Cache for session check results to avoid excessive API calls
    _lastCheckTime: 0,
    _lastCheckResult: null,
    _checkCacheTimeout: 30000, // 30 seconds cache
    _sessionValid: true,
    _pendingValidation: null, // Promise deduplication for concurrent validateSession calls
    
    // User activity tracking
    _userActive: false,
    _lastActivity: 0,
    _inactivityTimeout: 300000, // 5 minutes of inactivity
    _activityThrottle: 1000, // Throttle activity detection to 1 second
    _activityUpdateMinInterval: 5000, // Only record activity at most every 5 seconds while active
    _lastActivityCheck: 0,
    _lastActivityUpdate: 0,
    
    // Modal state tracking
    _modalShown: false,
    
    // Session maintenance
    _maintenanceInterval: null,
    _maintenanceIntervalMs: 600000, // 10 minutes - check for session maintenance

    // Session expiration tracking
    _expiresAt: null,
    _magicLinkExpiresAt: null,
    _lastExpiresIn: null,
    _sessionDurationMs: null,
    _refreshThresholdMs: 720000, // default 12 minutes before expiry
    _refreshCooldownMs: 60000, // 1 minute cooldown between refresh attempts
    _lastRefreshTime: 0,
    _refreshInProgress: false,
    _usedMagicLink: false,
    _focusCheckThrottleMs: 15000, // Avoid refetching on quick tab switches
    _initialCheckDone: false, // Track if initial check used shared /api/user/init data
    
    /**
     * Initializes the SessionManager with activity detection
     * Skips initialization on auth pages where user is not logged in
     */
    init() {
        const authPages = ['/login', '/register', '/logout', '/auth/'];
        const currentPath = window.location.pathname;

        if (authPages.some(page => currentPath.startsWith(page))) {
            return;
        }

        this.setupActivityDetection();
        this.setupVisibilityDetection();
        this.setupSessionMaintenance();
    },
    
    /**
     * Sets up user activity detection with throttling
     */
    setupActivityDetection() {
        const activityEvents = ['mousemove', 'keydown', 'keyup', 'input', 'click', 'scroll', 'touchstart', 'focus'];
        
        const throttledActivityHandler = this.throttle(() => {
            const now = Date.now();
            const timeSinceLastActivity = now - this._lastActivity;
            const wasInactive = (!this._userActive && timeSinceLastActivity > this._focusCheckThrottleMs) ||
                timeSinceLastActivity > this._inactivityTimeout;
            this._lastActivity = now;
            const sinceLastUpdate = now - this._lastActivityUpdate;
            if (!wasInactive && sinceLastUpdate < this._activityUpdateMinInterval) {
                return;
            }
            this._userActive = true;
            this._lastActivityUpdate = now;
            if (wasInactive) {
                this.forceNextCheck();
                this.validateSession(true).catch(error => console.error('Error validating session after inactivity:', error));
            }
        }, this._activityThrottle);

        
        activityEvents.forEach(event => {
            document.addEventListener(event, throttledActivityHandler, { passive: true });
        });
        
        // Mark as initially active
        this._userActive = true;
        this._lastActivity = Date.now();
        this._lastActivityUpdate = 0;

    },

    /**
     * Sets up visibility and focus detection
     */
    setupVisibilityDetection() {
        window.addEventListener('focus', () => {
            const shouldCheck = this.shouldValidateOnFocus();
            this.wakeUp(false);
            if (shouldCheck) {
                this.validateSession(false).catch(error => console.error('Error validating session on focus:', error));
            }
        });

        window.addEventListener('blur', () => {
            this.markInactive();
        });

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                const shouldCheck = this.shouldValidateOnFocus();
                this.wakeUp(false);
                if (shouldCheck) {
                    this.validateSession(false).catch(error => console.error('Error validating session on visibility change:', error));
                }
            } else {
                this.markInactive();
            }
        });
    },

    /**
     * Sets up automatic session maintenance for active users
     */
    setupSessionMaintenance() {
        // Clear any existing interval
        if (this._maintenanceInterval) {
            clearInterval(this._maintenanceInterval);
        }

        // Set up periodic session maintenance
        this._maintenanceInterval = setInterval(async () => {
            if (this.isUserActive()) {
                await this.maintainSession();
            }
        }, this._maintenanceIntervalMs);
    },
    
    /**
     * Throttle function to limit event frequency
     */
    throttle(func, limit) {
        let inThrottle;
        return function() {
            const args = arguments;
            const context = this;
            if (!inThrottle) {
                func.apply(context, args);
                inThrottle = true;
                setTimeout(() => inThrottle = false, limit);
            }
        }
    },
    
    /**
     * Checks if user is currently active
     * @returns {boolean}
     */
    isUserActive() {
        const now = Date.now();
        const timeSinceLastActivity = now - this._lastActivity;
        
        if (timeSinceLastActivity > this._inactivityTimeout) {
            this._userActive = false;
            return false;
        }
        
        return this._userActive;
    },
    
    /**
     * Validates current session with server (only if user is active)
     * Uses intelligent caching to avoid excessive requests
     * @param {boolean} forceCheck Force check even if user inactive
     * @returns {Promise<boolean>} true if session is valid
     */
    async validateSession(forceCheck = false) {
        const isCurrentlyActive = this.isUserActive();

        if (!forceCheck && !isCurrentlyActive) {
            return this._sessionValid;
        }

        const now = Date.now();

        if (!forceCheck && this._lastCheckResult !== null &&
            (now - this._lastCheckTime) < this._checkCacheTimeout) {
            return this._lastCheckResult;
        }

        // Deduplicate concurrent non-force checks: piggyback on in-flight validation.
        // Force-check calls (mutating requests) always get their own execution to preserve
        // their stricter error-handling semantics (hard failure instead of grace fallback).
        if (!forceCheck && this._pendingValidation) {
            return this._pendingValidation;
        }

        const validationPromise = this._executeValidation(forceCheck);
        if (!forceCheck) {
            this._pendingValidation = validationPromise;
        }
        try {
            return await validationPromise;
        } finally {
            if (this._pendingValidation === validationPromise) {
                this._pendingValidation = null;
            }
        }
    },

    async _executeValidation(forceCheck) {
        let now;
        try {
            let data;

            // On initial load, try to use shared /api/user/init data from ThemeManager
            // This avoids a duplicate HTTP request
            if (!forceCheck && !this._initialCheckDone) {
                this._initialCheckDone = true;

                // Wait for shared promise if ThemeManager already started the request
                if (window.__userInitPromise) {
                    try {
                        const initData = await window.__userInitPromise;
                        data = initData.session;
                    } catch (e) {
                        // Fall through to regular check
                        data = null;
                    }
                } else if (window.__userInitData?.session) {
                    // Data already cached
                    data = window.__userInitData.session;
                }
            }

            // Regular session check (forceCheck, refresh, or no cached data)
            if (!data) {
                const response = await fetch('/api/check-session', {
                    method: 'GET',
                    credentials: 'include',
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest'
                    }
                });

                if (!response.ok) {
                    if (response.status === 401 || response.status === 302) {
                        this._lastCheckTime = Date.now();
                        this._lastCheckResult = false;
                        this._sessionValid = false;
                        this.clearExpirationTracking();
                        this.handleSessionExpiry();
                        return false;
                    }
                    throw new Error(`HTTP ${response.status}`);
                }

                const contentType = response.headers.get('content-type');
                if (!contentType || !contentType.includes('application/json')) {
                    this._lastCheckTime = Date.now();
                    this._lastCheckResult = false;
                    this._sessionValid = false;
                    this.clearExpirationTracking();
                    this.handleSessionExpiry();
                    return false;
                }

                data = await response.json();
            }

            // Capture timestamp after network call resolves, not at function entry,
            // so the 30s cache window starts from when we actually got fresh data
            now = Date.now();
            this._lastCheckTime = now;
            this._lastCheckResult = !data.expired;
            this._sessionValid = this._lastCheckResult;

            if (data.expired) {
                this.clearExpirationTracking();
                this.handleSessionExpiry();
                return false;
            }

            this.updateExpirationTracking(data);

            if (!this._refreshInProgress && this.shouldRefreshSoon()) {
                await this.refreshSession();
            }
            return true;
        } catch (error) {
            console.error('Error checking session:', error);

            if (error instanceof SyntaxError && error.message.includes('JSON')) {
                this._lastCheckTime = Date.now();
                this._lastCheckResult = false;
                this._sessionValid = false;
                this.clearExpirationTracking();
                this.handleSessionExpiry();
                return false;
            }

            const timeSinceLastCheck = Date.now() - this._lastCheckTime;
            if (!forceCheck && this._lastCheckResult !== null && timeSinceLastCheck < 60000) {
                return this._lastCheckResult;
            }

            this._lastCheckResult = false;
            this._sessionValid = false;
            return false;
        }
    },

    updateExpirationTracking(sessionData) {
        const now = Date.now();

        if (typeof sessionData.expires_in === 'number') {
            const clampedSeconds = Math.max(sessionData.expires_in, 0);
            this._lastExpiresIn = sessionData.expires_in;
            this._expiresAt = now + clampedSeconds * 1000;

            const durationMs = clampedSeconds * 1000;
            if (this._sessionDurationMs === null || durationMs >= this._sessionDurationMs) {
                this._sessionDurationMs = durationMs;
                const dynamicThreshold = Math.min(this._sessionDurationMs * 0.2, 15 * 60 * 1000);
                this._refreshThresholdMs = Math.max(120000, Math.floor(dynamicThreshold));
            }
        } else {
            this._lastExpiresIn = null;
            this._expiresAt = null;
        }

        if (typeof sessionData.magic_link_expires_in === 'number') {
            const clampedMagic = Math.max(sessionData.magic_link_expires_in, 0);
            this._magicLinkExpiresAt = now + clampedMagic * 1000;
        } else {
            this._magicLinkExpiresAt = null;
        }

        this._usedMagicLink = Boolean(sessionData.used_magic_link);
    },

    clearExpirationTracking() {
        this._expiresAt = null;
        this._magicLinkExpiresAt = null;
        this._lastExpiresIn = null;
        this._usedMagicLink = false;
        this._lastRefreshTime = 0;
    },

    shouldRefreshSoon() {
        if (!this._expiresAt) {
            return false;
        }

        const now = Date.now();
        const timeLeft = this._expiresAt - now;

        if (timeLeft <= 0) {
            return true;
        }

        if (timeLeft <= this._refreshThresholdMs) {
            if (now - this._lastRefreshTime < this._refreshCooldownMs) {
                return false;
            }
            return true;
        }

        return false;
    },

    shouldValidateOnFocus() {
        if (this._lastCheckResult === null || this._lastCheckTime === 0) {
            return true;
        }

        const now = Date.now();
        const timeSinceLastCheck = now - this._lastCheckTime;

        if (timeSinceLastCheck >= this._checkCacheTimeout) {
            return true;
        }

        return timeSinceLastCheck >= this._focusCheckThrottleMs;
    },

    /**
     * Wraps any function with session validation
     * Always forces validation for critical actions
     * @param {Function} fn Function to wrap
     * @param {boolean} showError Whether to show error message if session invalid
     * @param {boolean} forceCheck Force session check even if user inactive (default: true for critical actions)
     * @returns {Function} Wrapped function
     */
    withSessionValidation(fn, showError = true, forceCheck = true) {
        return async (...args) => {
            // Always force validation for critical actions by default
            const isValid = await this.validateSession(forceCheck);
            if (isValid) {
                return fn.apply(this, args);
            }
            // Don't call handleSessionExpiry() here as validateSession already did
        };
    },
    
    /**
     * Wraps fetch calls with automatic session validation
     * Always validates for server requests
     * @param {string} url URL to fetch
     * @param {Object} options Fetch options
     * @returns {Promise<Response>} Fetch response
     */
    async secureFetch(url, options = {}) {
        const method = (options.method || 'GET').toUpperCase();
        const isMutating = method !== 'GET' && method !== 'HEAD';
        const isValid = await this.validateSession(isMutating);
        if (!isValid) {
            throw new Error('Session expired');
        }

        // Build headers with CSRF protection
        const headers = {
            'X-Requested-With': 'XMLHttpRequest',
            ...options.headers
        };

        // Add user API keys if available (for AI calls)
        // Only add to message/AI related endpoints
        if (window.userCredentials && url.includes('/messages')) {
            try {
                const keysBase64 = await window.userCredentials.getKeysForRequest();
                if (keysBase64) {
                    headers['X-User-API-Keys'] = keysBase64;
                }
            } catch (e) {
                console.debug('Could not get user API keys:', e);
            }
        }

        // Ensure credentials and CSRF protection
        const secureOptions = {
            ...options,
            credentials: 'include',
            headers: headers
        };

        try {
            const response = await fetch(url, secureOptions);
            
            // Check if response indicates session expiry
            if (response.status === 401) {
                try {
                    const data = await response.json();
                    if (data.redirect) {
                        this.handleSessionExpiry();
                        return null;
                    }
                } catch (e) {
                    // Response might not be JSON
                    this.handleSessionExpiry();
                    return null;
                }
            }
            
            return response;
        } catch (error) {
            console.error('Secure fetch error:', error);
            throw error;
        }
    },
    
    /**
     * Handles session expiry with user-friendly experience
     */
    handleSessionExpiry() {
        // Prevent multiple modals from showing
        if (this._modalShown) {
            return;
        }

        // Invalidate cache immediately
        this.invalidateCache();
        this._modalShown = true;

        const message = 'Your session has expired. Click "Go to Login" to authenticate again.';

        // Try NotificationModal first (globally available via base.html)
        if (typeof NotificationModal !== 'undefined') {
            NotificationModal.confirm(
                'Session Expired',
                message,
                () => this.redirectToLogin(),
                () => { this._modalShown = false; },
                { confirmText: 'Go to Login', cancelText: 'Cancel' }
            );

            // Reset modal flag when modal is hidden
            setTimeout(() => {
                const modal = document.getElementById('notificationModal');
                if (modal) {
                    modal.addEventListener('hidden.bs.modal', () => {
                        this._modalShown = false;
                    }, { once: true });
                }
            }, 100);
        }
        // Last resort: native confirm
        else {
            const shouldRedirect = confirm(message + ' Click OK to go to login.');
            this._modalShown = false;
            if (shouldRedirect) {
                this.redirectToLogin();
            }
        }
    },
    
    /**
     * Redirects to login page
     */
    redirectToLogin() {
        window.location.href = '/login';
    },
    
    /**
     * Invalidates the session cache (force recheck on next validation)
     */
    invalidateCache() {
        this._lastCheckTime = 0;
        this._lastCheckResult = null;
        this._sessionValid = false;
        this._modalShown = false; // Reset modal flag
        this.clearExpirationTracking();
    },
    
    /**
     * Quick sync check if session appears valid based on last check
     * This is a quick sync check - doesn't make API calls
     * @returns {boolean}
     */
    isSessionValid() {
        return this._sessionValid && this._lastCheckResult;
    },
    
    /**
     * Force a session check on next validation
     */
    forceNextCheck() {
        this.invalidateCache();
    },
    
    markInactive() {
        this._userActive = false;
    },

    /**
     * Wake up from inactivity - mark user as active and optionally check session
     * @param {boolean} checkSession Whether to immediately check session
     */
    wakeUp(checkSession = false) {
        this._userActive = true;
        this._lastActivity = Date.now();
        
        if (checkSession) {
            return this.validateSession(true);
        }
    },

    /**
     * Refresh the current session JWT token
     * @returns {Promise<boolean>} true if refresh was successful
     */
    async refreshSession() {
        if (this._refreshInProgress) {
            return true;
        }

        this._refreshInProgress = true;
        try {
            const response = await fetch('/api/refresh-session', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });

            if (!response.ok) {
                return false;
            }

            const data = await response.json();

            if (data.success) {
                this.invalidateCache();
                this._sessionValid = true;
                this._lastCheckResult = true;
                this._lastCheckTime = Date.now();
                this._lastRefreshTime = Date.now();
                return true;
            }

            return false;
        } catch (error) {
            console.error('Error refreshing session:', error);
            return false;
        } finally {
            this._refreshInProgress = false;
        }
    },

    /**
     * Auto-refresh session when user is active and session is close to expiring
     * This should be called periodically for active users
     * @returns {Promise<boolean>} true if session is valid (either was valid or successfully refreshed)
     */
    async maintainSession() {
        if (!this.isUserActive()) {
            return this._sessionValid;
        }

        const isCurrentlyValid = await this.validateSession(true);

        if (!isCurrentlyValid) {
            return false;
        }

        if (this.shouldRefreshSoon()) {
            const refreshed = await this.refreshSession();
            if (refreshed) {
                await this.validateSession(true);
            }
        }

        return true;
    }
};

/**
 * Global function to wrap existing functions with session validation
 * Usage: const secureFunction = withSession(originalFunction);
 */
function withSession(fn, showError = true, forceCheck = false) {
    return SessionManager.withSessionValidation(fn, showError, forceCheck);
}

/**
 * Global function for secure fetch operations
 * Usage: const response = await secureFetch('/api/endpoint', options);
 */
function secureFetch(url, options = {}) {
    return SessionManager.secureFetch(url, options);
}

// Initialize session manager when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => SessionManager.init());
} else {
    SessionManager.init();
}

// Export for use in other modules
window.SessionManager = SessionManager;
window.withSession = withSession;
window.secureFetch = secureFetch;