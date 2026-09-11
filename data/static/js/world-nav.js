(function() {
    'use strict';

    var nav = document.getElementById('aurvek-world-nav');
    if (!nav) return;

    // =========================================================================
    // Auto-hide on scroll
    // =========================================================================
    var lastScrollY = 0;
    var ticking = false;

    window.addEventListener('scroll', function() {
        if (!ticking) {
            requestAnimationFrame(function() {
                var currentScrollY = window.scrollY;
                if (currentScrollY > lastScrollY && currentScrollY > 80) {
                    nav.classList.add('aurvek-world-nav--hidden');
                } else {
                    nav.classList.remove('aurvek-world-nav--hidden');
                }
                lastScrollY = currentScrollY;
                ticking = false;
            });
            ticking = true;
        }
    });

    // =========================================================================
    // Crossfade page transitions
    // =========================================================================
    // Fade-in on load
    document.body.classList.add('aurvek-world-page');

    // Re-trigger fade-in on BFCache restore (browser back)
    window.addEventListener('pageshow', function(e) {
        if (e.persisted) {
            document.body.classList.remove('aurvek-world-fade-out');
            document.body.classList.remove('aurvek-world-page');
            // Force reflow to restart animation
            void document.body.offsetWidth;
            document.body.classList.add('aurvek-world-page');
        }
    });

    // Intercept world navigation links for smooth crossfade
    document.addEventListener('click', function(e) {
        var link = e.target.closest('a[href*="/welcome/"]');
        if (!link) return;
        e.preventDefault();
        var href = link.href;
        document.body.classList.add('aurvek-world-fade-out');
        setTimeout(function() { window.location.href = href; }, 250);
    });

    // =========================================================================
    // Shared dropdown helpers
    // =========================================================================
    var switcherBtn = document.getElementById('world-switcher-btn');
    var dropdown = document.getElementById('world-switcher-dropdown');
    var chevron = document.getElementById('world-switcher-chevron');
    var currentName = document.getElementById('world-current-name');

    var settingsBtn = document.getElementById('world-settings-btn');
    var settingsDropdown = document.getElementById('world-settings-dropdown');

    function closeAllDropdowns() {
        if (dropdown) dropdown.classList.remove('aurvek-world-nav-dropdown--open');
        if (chevron) chevron.classList.remove('aurvek-world-nav-chevron--open');
        if (settingsDropdown) settingsDropdown.classList.remove('aurvek-world-nav-dropdown--open');
    }

    // =========================================================================
    // Product Switcher Dropdown
    // =========================================================================
    if (switcherBtn) {
        switcherBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            var isOpen = dropdown.classList.contains('aurvek-world-nav-dropdown--open');
            closeAllDropdowns();
            if (!isOpen) {
                dropdown.classList.add('aurvek-world-nav-dropdown--open');
                chevron.classList.add('aurvek-world-nav-chevron--open');
            }
        });
    }

    // =========================================================================
    // Settings Dropdown
    // =========================================================================
    if (settingsBtn) {
        settingsBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            var isOpen = settingsDropdown.classList.contains('aurvek-world-nav-dropdown--open');
            closeAllDropdowns();
            if (!isOpen) {
                settingsDropdown.classList.add('aurvek-world-nav-dropdown--open');
            }
        });
    }

    // Close all dropdowns on outside click or Escape
    document.addEventListener('click', closeAllDropdowns);
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') closeAllDropdowns();
    });

    // =========================================================================
    // "Classic Home" button
    // =========================================================================
    var homeBtn = document.getElementById('world-classic-home-btn');
    if (homeBtn) {
        homeBtn.addEventListener('click', function() {
            window.location.href = '/home';
        });
    }

    // =========================================================================
    // Populate from window.__aurvekWorlds
    // =========================================================================
    var worldsData = window.__aurvekWorlds || {};

    // Set current product name
    if (worldsData.current && currentName) {
        currentName.textContent = worldsData.current.name || window.AurvekI18n.t('public_shell.home');
    }

    // ── Back to Pack button ──
    var backBtn = document.getElementById('world-back-to-pack');
    var backName = document.getElementById('world-back-to-pack-name');
    if (backBtn && backName && worldsData.current && worldsData.current.parent_pack) {
        var pack = worldsData.current.parent_pack;
        backBtn.href = '/welcome/' + pack.type + '/' + pack.id;
        backName.textContent = pack.name;
        backBtn.style.display = '';
    }

    // ── Build dropdown items ──
    var welcomeProducts = (worldsData.products || []).filter(function(p) { return p.has_welcome; });
    if (welcomeProducts.length > 0 && dropdown) {
        welcomeProducts.forEach(function(product) {

            var item = document.createElement('a');
            item.href = '/welcome/' + product.type + '/' + product.id;
            item.className = 'aurvek-world-nav-dropdown-item';

            // Mark active item
            if (worldsData.current &&
                product.type === worldsData.current.type &&
                String(product.id) === String(worldsData.current.id)) {
                item.classList.add('aurvek-world-nav-dropdown-item--active');
            }

            // Avatar or placeholder
            if (product.avatar_url) {
                var avatar = document.createElement('img');
                avatar.src = product.avatar_url;
                avatar.className = 'aurvek-world-nav-dropdown-avatar';
                avatar.alt = '';
                avatar.loading = 'lazy';
                avatar.onerror = function() { this.style.display = 'none'; };
                item.appendChild(avatar);
            } else {
                var placeholder = document.createElement('div');
                placeholder.className = 'aurvek-world-nav-dropdown-avatar-placeholder';
                placeholder.textContent = (product.name || '?')[0].toUpperCase();
                item.appendChild(placeholder);
            }

            // Product name
            var name = document.createElement('span');
            name.className = 'aurvek-world-nav-dropdown-name';
            name.textContent = product.name;
            item.appendChild(name);

            dropdown.appendChild(item);
        });
    } else {
        // Single product or no products - hide the chevron, disable switcher
        if (chevron) chevron.style.display = 'none';
        if (switcherBtn) {
            switcherBtn.style.cursor = 'default';
        }
    }
})();
