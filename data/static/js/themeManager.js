/**
 * ThemeManager - Centralized theme management for all pages
 * Replaces duplicated inline theme code across templates
 *
 * RELATIONSHIP WITH theme-loader.js:
 * ---------------------------------
 * theme-loader.js: Runs synchronously in <head> BEFORE page renders.
 *   - Reads theme from localStorage
 *   - Writes the correct CSS <link> tag using document.write()
 *   - Sets window.__themeLoaderExecuted = true and window.__themeLoaderTheme
 *   - Prevents FOUC (Flash of Unstyled Content)
 *
 * themeManager.js (this file): Runs after DOMContentLoaded.
 *   - Handles RUNTIME theme switching (user clicks theme selector)
 *   - Manages forced themes (Phase 5 white-label)
 *   - Binds click handlers to theme options
 *   - Handles special effects (xmas snowflakes, terminal class)
 *   - Manages sidebar submenu positioning
 *
 * The init() method checks if theme-loader.js already loaded the theme
 * and skips redundant CSS loading. loadCSS() is only called when the
 * user actively switches themes at runtime.
 */
const ThemeManager = {
    themes: [
        'default', 'writer', 'coder', 'light', 'xmas', 'halloween',
        'valentinesday', 'nekoglass', 'frutigeraero', 'memphis',
        'terminal', 'eink', 'katarishoji'
    ],

    // Phase 5: White-label theme enforcement
    forcedTheme: null,
    themeSelectorDisabled: false,

    /**
     * Derives CSS path from current URL
     *
     * UNIFIED THEME SYSTEM (2026-01-29):
     * - Chat pages: keep separate CSS (complex layout)
     * - All other pages (including index/login): use unified themes/
     *
     * @returns {string} CSS path prefix (without theme suffix)
     */
    getCSSPath() {
        let path = window.location.pathname.replace(/^\/|\/$/g, '');

        // === EXCLUDED FROM UNIFIED SYSTEM ===

        // Chat - complex layout, keeps separate CSS
        if (path === 'chat' || path.startsWith('chat/')) {
            return '/static/css/chat/chat-';
        }

        // === UNIFIED THEME SYSTEM ===
        // All other pages (including index/login) use the new unified themes
        return '/static/css/themes/';
    },

    /**
     * Gets static URL with CDN support
     * @param {string} path - The static file path
     * @returns {string} Full URL
     */
    getStaticUrl(path) {
        if (window.CDN_CONFIG && window.CDN_CONFIG.enabled) {
            let cdnPath = path;
            if (cdnPath.startsWith('/static/')) {
                cdnPath = cdnPath.substring(7);
            } else if (cdnPath.startsWith('/static')) {
                cdnPath = cdnPath.substring(7);
            }
            if (!cdnPath.startsWith('/')) {
                cdnPath = '/' + cdnPath;
            }
            return window.CDN_CONFIG.baseUrl + cdnPath;
        }
        return path;
    },

    /**
     * Gets the CSS pattern for finding theme stylesheets
     * Extracts the base pattern from getCSSPath() (e.g., 'chat/chat-' or 'index-')
     * @returns {string} Pattern to match in href
     */
    getCSSPattern() {
        const cssPath = this.getCSSPath();
        // Extract pattern after /static/css/ (e.g., 'chat/chat-' or 'index-')
        const match = cssPath.match(/\/static\/css\/(.+)$/);
        return match ? match[1] : 'index-';
    },

    /**
     * Disables all theme CSS files matching the current page pattern
     * @param {string} pattern - Pattern to match (e.g., 'chat/chat-' or 'index-')
     */
    disableAllThemeCSS(pattern) {
        document.querySelectorAll('link[rel="stylesheet"]').forEach(link => {
            if (link.href && link.href.includes(pattern)) {
                link.disabled = true;
            }
        });
    },

    /**
     * Loads theme CSS file for RUNTIME theme switching
     *
     * NOTE: This is only used when the user actively switches themes.
     * Initial theme loading is handled by theme-loader.js in the <head>.
     *
     * Uses disable/enable strategy instead of remove to properly swap themes
     * @param {string} theme - Theme name
     * @returns {Promise<HTMLLinkElement>}
     */
    loadCSS(theme) {
        return new Promise((resolve, reject) => {
            const cssPath = this.getCSSPath();
            const cssPattern = this.getCSSPattern();
            let fullPath = this.getStaticUrl(`${cssPath}${theme}.css`);

            // Cache-busting
            if (window.STATIC_HASHES) {
                const hashKey = `${cssPath}${theme}.css`;
                const hash = window.STATIC_HASHES[hashKey];
                if (hash) {
                    fullPath += (fullPath.includes('?') ? '&' : '?') + 'v=' + hash;
                }
            }

            // Disable ALL existing theme CSS files for this page type
            this.disableAllThemeCSS(cssPattern);

            // Check if the desired theme is already loaded (just needs enabling)
            // This includes the CSS loaded by theme-loader.js (has id="theme-css")
            const existingLink = document.querySelector(`link[href*="${cssPattern}${theme}.css"]`);
            if (existingLink) {
                existingLink.disabled = false;
                resolve(existingLink);
                return;
            }

            // Create new link for the theme
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = fullPath;

            link.onload = () => {
                resolve(link);
            };

            link.onerror = () => {
                console.error('ThemeManager: Failed to load theme:', theme);
                // Fallback to default
                if (theme !== 'default') {
                    this.loadCSS('default').then(resolve).catch(reject);
                } else {
                    reject(new Error('Failed to load default theme'));
                }
            };

            const commonLink = document.querySelector('link[href*="chat-common.css"]');
            if (commonLink) {
                document.head.insertBefore(link, commonLink);
            } else {
                document.head.appendChild(link);
            }
        });
    },

    /**
     * Sets theme and saves to localStorage
     * @param {string} theme - Theme name
     * @param {boolean} reload - Whether to reload page after setting theme (default: true)
     */
    setTheme(theme, reload = true) {
        // Phase 5: Respect forced theme from creator
        if (this.forcedTheme && reload) {
            // User is trying to change theme but it's locked
            console.log('ThemeManager: Theme is locked to', this.forcedTheme);
            return;
        }

        if (!this.themes.includes(theme)) {
            console.warn('ThemeManager: Unknown theme:', theme);
            theme = 'default';
        }

        // Only save to localStorage if theme is not forced
        if (!this.forcedTheme) {
            localStorage.setItem('theme', theme);
        }

        if (reload) {
            // Save current conversation ID before reload (for chat page)
            if (typeof currentConversationId !== 'undefined' && currentConversationId) {
                localStorage.setItem('restoreConversationId', currentConversationId);
            }
            // Reload page to cleanly apply new theme
            location.reload();
        } else {
            // Apply without reload (used during init fallback when theme-loader.js didn't run)
            this.loadCSS(theme)
                .then(() => {
                    this.applyThemeEffects(theme);
                    this.updateDropdown(theme);
                })
                .catch(err => console.error('ThemeManager: Error setting theme:', err));
        }
    },

    /**
     * Updates dropdown to show active theme
     * @param {string} theme - Active theme name
     */
    updateDropdown(theme) {
        // Remove active class from all options
        document.querySelectorAll('.theme-option').forEach(option => {
            option.classList.remove('active');
        });

        // Add active class to ALL matching theme options (chat has mobile + desktop dropdowns)
        document.querySelectorAll(`.theme-option[data-theme="${theme}"]`).forEach(option => {
            option.classList.add('active');
        });
    },

    /**
     * Sets up theme submenu for sidebar (chat page)
     * Moves submenu to body on hover to escape display:none from parent dropdown
     */
    setupSidebarSubmenu() {
        const sidebar = document.getElementById('sidebar');
        if (!sidebar) return;

        const dropends = sidebar.querySelectorAll('.dropend');
        dropends.forEach(dropend => {
            const submenu = dropend.querySelector('.settings-submenu');
            if (!submenu) return;

            const originalParent = submenu.parentElement;
            let isSubmenuVisible = false;

            const showSubmenu = () => {
                if (isSubmenuVisible) return;
                isSubmenuVisible = true;

                // Move to body to escape display:none
                document.body.appendChild(submenu);

                // Position it
                const rect = dropend.getBoundingClientRect();
                const viewportHeight = window.innerHeight;
                const viewportWidth = window.innerWidth;

                // Position to the right of the trigger
                submenu.style.position = 'fixed';
                submenu.style.left = (rect.right + 4) + 'px';
                submenu.style.zIndex = '9999';

                // Align bottom of submenu near the trigger, expanding upward
                let bottom = viewportHeight - rect.bottom;
                if (bottom < 10) bottom = 10;

                submenu.style.bottom = bottom + 'px';
                submenu.style.top = 'auto';
                submenu.style.display = 'block';

                // Keep the flyout inside narrow viewports after measuring its real width.
                const submenuWidth = submenu.getBoundingClientRect().width;
                const maxLeft = Math.max(8, viewportWidth - submenuWidth - 8);
                submenu.style.left = Math.max(8, Math.min(rect.right + 4, maxLeft)) + 'px';
            };

            const hideSubmenu = () => {
                isSubmenuVisible = false;
                submenu.style.display = 'none';
                // Return to original parent
                originalParent.appendChild(submenu);
            };

            // Show on hover over the dropend trigger
            dropend.addEventListener('mouseenter', showSubmenu);
            const toggle = dropend.querySelector('.dropdown-toggle');
            if (toggle) {
                toggle.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    showSubmenu();
                });
            }

            // Keep visible while hovering submenu
            submenu.addEventListener('mouseenter', () => {
                isSubmenuVisible = true;
            });

            // Hide when leaving both
            dropend.addEventListener('mouseleave', (e) => {
                // Check if moving to submenu
                setTimeout(() => {
                    if (!submenu.matches(':hover') && !dropend.matches(':hover')) {
                        hideSubmenu();
                    }
                }, 100);
            });

            submenu.addEventListener('mouseleave', (e) => {
                setTimeout(() => {
                    if (!submenu.matches(':hover') && !dropend.matches(':hover')) {
                        hideSubmenu();
                    }
                }, 100);
            });

            // Also hide when clicking outside
            document.addEventListener('click', (e) => {
                if (isSubmenuVisible && !submenu.contains(e.target) && !dropend.contains(e.target)) {
                    hideSubmenu();
                }
            });
        });
    },

    /**
     * Checks for forced theme configuration from the server
     * Phase 5: White-label theme enforcement
     * Uses combined /api/user/init endpoint to reduce HTTP requests.
     * Session data is cached in window.__userInitData for SessionManager to use.
     * @returns {Promise<void>}
     */
    async checkForcedTheme() {
        try {
            // Use shared promise to avoid duplicate requests
            if (!window.__userInitPromise) {
                let initUrl = '/api/user/init';
                const storeMatch = window.location.pathname.match(/^\/store\/([^/]+)/);
                if (storeMatch) {
                    initUrl += `?context_type=storefront&context_id=${encodeURIComponent(storeMatch[1])}`;
                }
                window.__userInitPromise = fetch(initUrl, {
                    method: 'GET',
                    credentials: 'include',
                    headers: { 'X-Requested-With': 'XMLHttpRequest' }
                }).then(async (response) => {
                    if (response.ok) {
                        const data = await response.json();
                        window.__userInitData = data;
                        return data;
                    }
                    throw new Error(`HTTP ${response.status}`);
                });
            }

            const data = await window.__userInitPromise;
            const config = data.theme || {};

            if (config.forced_theme && this.themes.includes(config.forced_theme)) {
                this.forcedTheme = config.forced_theme;
            }
            this.themeSelectorDisabled = config.disable_theme_selector || false;
        } catch (error) {
            // If the API fails, just use normal theme behavior
            console.warn('ThemeManager: Could not fetch user init:', error);
        }
    },

    /**
     * Hides theme selector UI elements when disabled by manager
     */
    hideThemeSelector() {
        // Hide theme options in navbar dropdowns
        document.querySelectorAll('[data-submenu="theme"]').forEach(el => {
            const parent = el.closest('.dropstart, .dropend');
            if (parent) {
                parent.style.display = 'none';
            }
        });

        // Hide any standalone theme dropdown triggers
        document.querySelectorAll('[data-bs-toggle="dropdown"]').forEach(el => {
            if (el.querySelector('.fa-palette')) {
                const parent = el.closest('li');
                if (parent) {
                    parent.style.display = 'none';
                }
            }
        });
    },

    /**
     * Initializes theme system
     * Call this on DOMContentLoaded
     *
     * If theme-loader.js already loaded the theme (window.__themeLoaderExecuted),
     * we skip CSS loading and only set up runtime handlers.
     */
    async init() {
        // Phase 5: Check for forced theme first
        await this.checkForcedTheme();

        // Determine which theme to use
        let activeTheme;
        if (this.forcedTheme) {
            // Use forced theme from creator
            activeTheme = this.forcedTheme;
        } else {
            // Use saved theme from localStorage
            activeTheme = localStorage.getItem('theme') || 'default';
        }

        // Check if theme-loader.js already loaded the theme
        const themeLoaderExecuted = window.__themeLoaderExecuted === true;
        const themeLoaderTheme = window.__themeLoaderTheme;

        if (themeLoaderExecuted) {
            // theme-loader.js already loaded CSS - skip redundant loading
            // But check if forced theme differs from what was loaded
            if (this.forcedTheme && this.forcedTheme !== themeLoaderTheme) {
                // Forced theme is different - need to switch
                // This happens when creator sets a forced theme after page load
                this.loadCSS(this.forcedTheme)
                    .then(() => this.applyThemeEffects(this.forcedTheme))
                    .catch(err => console.error('ThemeManager: Error applying forced theme:', err));
            } else {
                // Theme already correct - just apply effects and update dropdown
                this.applyThemeEffects(activeTheme);
            }
            this.updateDropdown(activeTheme);
        } else {
            // theme-loader.js did NOT run (fallback for pages without it)
            // Load theme the old way
            if (activeTheme !== 'default') {
                this.setTheme(activeTheme, false);
            } else {
                this.updateDropdown('default');
            }
        }

        // Bind click handlers for theme options (only if not disabled)
        if (!this.themeSelectorDisabled) {
            document.querySelectorAll('.theme-option').forEach(option => {
                option.addEventListener('click', (e) => {
                    e.preventDefault();
                    // Don't allow changing if forced
                    if (this.forcedTheme) {
                        console.log('ThemeManager: Theme is locked by administrator');
                        return;
                    }
                    const theme = option.getAttribute('data-theme');
                    if (theme) {
                        this.setTheme(theme);
                    }
                });
            });
        } else {
            // Hide theme selector if disabled
            this.hideThemeSelector();
        }

        // Setup sidebar submenu for chat page
        this.setupSidebarSubmenu();

        // Setup click toggles for navbar submenus
        this.setupSubmenuToggle();
    },

    /**
     * Sets up click toggles for settings submenus on desktop and mobile
     * Bootstrap doesn't automatically handle nested dropstart submenus
     */
    setupSubmenuToggle() {
        document.querySelectorAll('.dropstart > .dropdown-toggle').forEach(toggle => {
            const dropstart = toggle.closest('.dropstart');
            const submenu = dropstart.querySelector('.settings-submenu');
            if (!submenu) return;
            const parentDropdown = dropstart.closest('.dropdown');

            toggle.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopImmediatePropagation(); // Stop Bootstrap from closing parent
                const opening = !dropstart.classList.contains('show');
                if (opening && parentDropdown) {
                    parentDropdown.querySelectorAll('.dropstart.show').forEach(other => {
                        if (other !== dropstart) {
                            other.classList.remove('show');
                            other.querySelector('.dropdown-toggle')?.setAttribute('aria-expanded', 'false');
                        }
                    });
                }
                dropstart.classList.toggle('show', opening);
                toggle.setAttribute('aria-expanded', String(opening));

                // Ensure parent dropdown stays open
                if (parentDropdown) {
                    parentDropdown.classList.add('show');
                    const parentMenu = parentDropdown.querySelector('.dropdown-menu');
                    if (parentMenu) parentMenu.classList.add('show');
                }
            });

            // Prevent clicks inside a settings submenu from closing parent dropdown
            submenu.addEventListener('click', (e) => {
                // Allow the option click to work but keep the parent dropdown open.
                e.stopPropagation();
            });

            // Close submenu when clicking outside
            document.addEventListener('click', (e) => {
                if (dropstart.classList.contains('show')) {
                    if (!dropstart.contains(e.target)) {
                        dropstart.classList.remove('show');
                        toggle.setAttribute('aria-expanded', 'false');
                    }
                }
            });

            // Close submenu when parent dropdown closes
            if (parentDropdown) {
                const observer = new MutationObserver(() => {
                    if (!parentDropdown.classList.contains('show')) {
                        dropstart.classList.remove('show');
                        toggle.setAttribute('aria-expanded', 'false');
                    }
                });
                observer.observe(parentDropdown, { attributes: true, attributeFilter: ['class'] });
            }
        });
    },

    /**
     * Applies theme-specific effects (body classes, snowflakes, etc.)
     * Called after theme CSS is loaded (either by theme-loader.js or loadCSS)
     * @param {string} theme - Theme name
     */
    applyThemeEffects(theme) {
        // Handle terminal theme body class
        document.body.classList.remove('terminal-theme');
        if (theme === 'terminal') {
            document.body.classList.add('terminal-theme');
        }

        // Handle xmas snowflakes
        if (theme === 'xmas' && typeof createSnowflakes === 'function') {
            createSnowflakes();
        }

        // Handle nekoglass glass effects
        if (theme === 'nekoglass') {
            NekoGlassControls.init();
        }
        // Cleanup handled by NekoGlassControls' theme:changed listener

        // Dispatch event for other components
        document.dispatchEvent(new CustomEvent('theme:changed', {
            detail: { theme }
        }));
    }
};

// Auto-initialize on DOMContentLoaded
document.addEventListener('DOMContentLoaded', () => {
    ThemeManager.init();
});
