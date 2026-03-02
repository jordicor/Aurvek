/**
 * Theme Loader - Synchronous theme loading to prevent FOUC (Flash of Unstyled Content)
 *
 * This script runs in the <head> BEFORE any CSS loads, reads the saved theme from
 * localStorage, and writes the correct CSS <link> tag. This prevents the "flash"
 * where the default theme shows briefly before switching to the user's theme.
 *
 * MUST be loaded synchronously (no defer/async) at the top of <head>
 */
(function() {
    'use strict';

    /**
     * Get the CSS path prefix for current page
     *
     * UNIFIED THEME SYSTEM (2026-01-29):
     * - Chat pages: keep separate CSS (complex layout)
     * - All other pages (including index/login): use unified themes/
     */
    function getCSSPath() {
        var path = window.location.pathname.replace(/^\/|\/$/g, '');

        // === EXCLUDED FROM UNIFIED SYSTEM ===

        // Chat - complex layout, keeps separate CSS
        if (path === 'chat' || path.indexOf('chat/') === 0) {
            return '/static/css/chat/chat-';
        }

        // === UNIFIED THEME SYSTEM ===
        // All other pages (including index/login) use the new unified themes
        return '/static/css/themes/';
    }

    /**
     * Get theme from localStorage
     */
    function getTheme() {
        try {
            return localStorage.getItem('theme') || 'default';
        } catch (e) {
            return 'default';
        }
    }

    // Main execution
    var theme = getTheme();
    var cssPath = getCSSPath();
    var cssUrl;

    // Use CDN if available (CDN_BASE_URL defined in base.html before this script)
    if (window.CDN_BASE_URL && window.CDN_BASE_URL.indexOf('http') === 0) {
        // CDN_BASE_URL is like "https://fstcdn.example.com/static"
        // cssPath is like "/static/css/themes/" - remove /static prefix
        var relativePath = cssPath.replace(/^\/static/, '');
        cssUrl = window.CDN_BASE_URL + relativePath + theme + '.css';
    } else {
        cssUrl = cssPath + theme + '.css';
    }

    // Write the CSS link tag - this blocks rendering until CSS loads
    document.write('<link rel="stylesheet" href="' + cssUrl + '" id="theme-css">');

    // Store for themeManager.js to know we already loaded the theme
    window.__themeLoaderExecuted = true;
    window.__themeLoaderTheme = theme;
})();
