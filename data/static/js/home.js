'use strict';

(function() {
    const mt = (key, params) => AurvekI18n.t('marketplace.' + key, params);
    const mh = (key, params) => escapeHtml(mt(key, params));
    let homeData = null;
    let welcomeMessages = null;
    const canManage = window._homeConfig && window._homeConfig.canManage;
    const marketplaceEnabled = !window._homeConfig || window._homeConfig.marketplaceEnabled !== false;
    const marketplaceDiscoveryEnabled = !window._homeConfig || window._homeConfig.marketplaceDiscoveryEnabled !== false;
    const minimizedWindows = new Set();
    let readTimers = {};

    document.addEventListener('DOMContentLoaded', init);

    async function init() {
        try {
            const resp = await fetch('/api/home');
            if (resp.status === 401) {
                window.location.href = '/login';
                return;
            }
            if (!resp.ok) throw new Error('Failed to load home data');
            homeData = await resp.json();

            // Load minimized state from preferences
            var prefs = homeData.home_preferences || {};
            var savedMinimized = prefs.minimized_windows || [];
            savedMinimized.forEach(function(id) { minimizedWindows.add(id); });

            render();
            if (marketplaceEnabled) {
                await loadWelcomeMessages();
            }
        } catch (e) {
            console.error('Home init error:', e);
        }
    }

    function render() {
        applyBranding();
        renderGreeting();
        renderChatInput();
        renderFavorites();
        if (marketplaceEnabled) {
            renderLatestPrompts();
            renderMyLibrary('prompts');
        }
        bindEvents();
        if (marketplaceEnabled) {
            applyMinimizedState();
        }
    }

    // ==================== Branding ====================
    function applyBranding() {
        var b = homeData.branding;
        if (b && b.brand_color_primary) {
            document.documentElement.style.setProperty('--home-accent', b.brand_color_primary);
        }
    }

    // ==================== Greeting ====================
    function renderGreeting() {
        var titleEl = document.getElementById('home-greeting-title');
        var subtitleEl = document.getElementById('home-greeting-subtitle');
        var hour = new Date().getHours();
        var greeting;
        if (hour < 6) greeting = mt('home.night', {name: homeData.user.username});
        else if (hour < 12) greeting = mt('home.morning', {name: homeData.user.username});
        else if (hour < 18) greeting = mt('home.afternoon', {name: homeData.user.username});
        else greeting = mt('home.evening', {name: homeData.user.username});

        var b = homeData.branding;
        if (b && b.company_name) {
            titleEl.textContent = b.company_name;
            subtitleEl.textContent = greeting;
        } else {
            titleEl.textContent = greeting;
            subtitleEl.textContent = mt('home.ready');
        }
    }

    // ==================== Chat Input ====================
    function renderChatInput() {
        var select = document.getElementById('home-prompt-select');
        var packs = homeData.packs || [];
        var prompts = homeData.prompts || [];
        var favIds = homeData.favorites || [];

        var favPrompts = prompts.filter(function(p) { return favIds.includes(p.id); });
        if (favPrompts.length) {
            var favGroup = document.createElement('optgroup');
            favGroup.label = '\u2605 ' + mt('explore.favorites');
            favPrompts.forEach(function(p) {
                var opt = document.createElement('option');
                opt.value = String(p.id);
                opt.textContent = p.name;
                favGroup.appendChild(opt);
            });
            select.appendChild(favGroup);
        }

        var myPrompts = prompts.filter(function(p) { return !!p.is_mine; });
        if (myPrompts.length) {
            var myGroup = document.createElement('optgroup');
            myGroup.label = mt('explore.my_prompts');
            myPrompts.forEach(function(p) {
                var opt = document.createElement('option');
                opt.value = String(p.id);
                opt.textContent = p.name;
                myGroup.appendChild(opt);
            });
            select.appendChild(myGroup);
        }

        if (prompts.length) {
            var allGroup = document.createElement('optgroup');
            allGroup.label = mt('home.all_prompts');
            prompts.forEach(function(p) {
                var opt = document.createElement('option');
                opt.value = String(p.id);
                opt.textContent = p.name;
                allGroup.appendChild(opt);
            });
            select.appendChild(allGroup);
        }

        var activePacks = packs.filter(function(pk) { return pk.prompt_count > 0; });
        if (activePacks.length) {
            var packGroup = document.createElement('optgroup');
            packGroup.label = mt('common.packs');
            activePacks.forEach(function(pk) {
                var opt = document.createElement('option');
                opt.value = 'pack:' + pk.id;
                opt.textContent = pk.name;
                packGroup.appendChild(opt);
            });
            select.appendChild(packGroup);
        }

        if (select.options.length === 2) {
            select.selectedIndex = 1;
        }
    }

    // ==================== Favorites ====================
    function renderFavorites() {
        var favIds = homeData.favorites || [];
        if (!favIds.length) return;

        var allPrompts = homeData.prompts || [];
        var favPrompts = allPrompts.filter(function(p) { return favIds.includes(p.id); });
        if (!favPrompts.length) return;

        var container = document.getElementById('home-favorites');
        container.style.display = '';
        container.innerHTML = favPrompts.map(function(p) {
            return '<button class="home-fav-chip" data-prompt-id="' + p.id + '" title="' + escapeHtml(p.name) + '">'
                + (p.image_url ? '<img src="' + escapeHtml(p.image_url) + '" alt="" class="home-fav-avatar">' : '<i class="fas fa-star"></i>')
                + '<span>' + escapeHtml(p.name) + '</span>'
                + '</button>';
        }).join('');
    }

    // ==================== Latest Prompts ====================
    function renderLatestPrompts() {
        if (!marketplaceEnabled || !marketplaceDiscoveryEnabled) return;

        var items = homeData.latest_prompts || [];
        if (!items.length) return;

        var section = document.getElementById('win-latest');
        if (!section) return;
        section.style.display = '';

        var now = new Date();
        var list = document.getElementById('home-latest-list');
        list.innerHTML = items.map(function(p) {
            var isNew = false;
            if (p.created_at) {
                var created = new Date(p.created_at + 'Z');
                isNew = (now - created) < 7 * 24 * 60 * 60 * 1000;
            }
            var badge = isNew ? '<span class="home-item-badge">' + mh('home.new') + '</span>' : '';
            var actions = '';
            if (p.has_welcome) {
                actions = '<div class="home-item-actions">'
                    + '<button class="home-item-icon" onclick="event.stopPropagation(); navigateToWelcome(\'prompt\', ' + p.id + ')" title="' + mh('home.visit_welcome') + '"><i class="fas fa-globe"></i></button>'
                    + '</div>';
            }
            return '<div class="home-item" data-prompt-id="' + p.id + '">'
                + '<a href="/explore" class="home-item-name">' + escapeHtml(p.name) + '</a>'
                + badge
                + actions
                + '</div>';
        }).join('');
    }

    // ==================== My Library ====================
    function renderMyLibrary(tab) {
        var list = document.getElementById('home-library-list');
        var emptyEl = document.getElementById('home-library-empty');
        var viewAllBtn = document.getElementById('home-view-all-btn');
        if (!list || !emptyEl || !viewAllBtn) return;
        var items;
        var maxItems = 5;

        if (tab === 'packs') {
            items = homeData.packs || [];
        } else {
            items = homeData.prompts || [];
        }

        emptyEl.querySelector('p').textContent = tab === 'packs' ? mt('home.no_packs') : mt('home.no_prompts');
        if (!items.length) {
            list.innerHTML = '';
            emptyEl.style.display = '';
            viewAllBtn.style.display = 'none';
            return;
        }

        emptyEl.style.display = 'none';
        var displayItems = items.slice(0, maxItems);

        list.innerHTML = displayItems.map(function(item) {
            var name = escapeHtml(item.name);
            var hasWelcome = item.has_welcome;
            var type = tab === 'packs' ? 'pack' : 'prompt';
            var actions = '';
            if (hasWelcome) {
                actions += '<button class="home-item-icon" onclick="navigateToWelcome(\'' + type + '\', ' + item.id + ')" title="' + mh('home.visit_welcome') + '"><i class="fas fa-globe"></i></button>';
            }
            if (tab === 'prompts') {
                actions += '<button class="home-item-icon" onclick="startChatWithPrompt(' + item.id + ')" title="' + mh('home.start_chat') + '"><i class="fas fa-comment-dots"></i></button>';
            }
            return '<div class="home-item">'
                + '<span class="home-item-name">' + name + '</span>'
                + '<div class="home-item-actions">' + actions + '</div>'
                + '</div>';
        }).join('');

        viewAllBtn.style.display = items.length > maxItems ? '' : 'none';
    }

    // ==================== View All Modal ====================
    function openViewAllModal(tab) {
        var overlay = document.getElementById('home-modal-overlay');
        if (!overlay) return;
        overlay.style.display = '';
        renderModalList(tab || 'prompts');
        var tabs = document.querySelectorAll('#home-modal-tabs .home-tab');
        tabs.forEach(function(t) {
            t.classList.toggle('active', t.getAttribute('data-tab') === (tab || 'prompts'));
        });
        var title = document.querySelector('.home-modal-title');
        if (title) title.textContent = (tab === 'packs') ? mt('home.all_packs') : mt('home.all_prompts');
    }

    function renderModalList(tab) {
        var list = document.getElementById('home-modal-list');
        if (!list) return;
        var items = (tab === 'packs') ? (homeData.packs || []) : (homeData.prompts || []);

        list.innerHTML = items.map(function(item) {
            var name = escapeHtml(item.name);
            var hasWelcome = item.has_welcome;
            var type = tab === 'packs' ? 'pack' : 'prompt';
            var actions = '';
            if (hasWelcome) {
                actions += '<button class="home-item-icon" onclick="navigateToWelcome(\'' + type + '\', ' + item.id + ')" title="' + mh('home.visit_welcome') + '"><i class="fas fa-globe"></i></button>';
            }
            if (tab === 'prompts') {
                actions += '<button class="home-item-icon" onclick="startChatWithPrompt(' + item.id + ')" title="' + mh('home.start_chat') + '"><i class="fas fa-comment-dots"></i></button>';
            }
            return '<div class="home-item">'
                + '<span class="home-item-name">' + name + '</span>'
                + '<div class="home-item-actions">' + actions + '</div>'
                + '</div>';
        }).join('');
    }

    function closeModal() {
        var overlay = document.getElementById('home-modal-overlay');
        if (overlay) overlay.style.display = 'none';
    }

    // ==================== Welcome Messages ====================

    async function loadWelcomeMessages() {
        if (!marketplaceEnabled) return;
        var welcomeWindow = document.getElementById('win-welcome');
        if (!welcomeWindow) return;

        try {
            var resp = await fetch('/api/home/welcome-messages');
            if (!resp.ok) return;
            var data = await resp.json();
            welcomeMessages = data.messages || [];

            if (!welcomeMessages.length) {
                // No messages: hide window, prune from minimized
                welcomeWindow.style.display = 'none';
                if (minimizedWindows.has('welcome')) {
                    minimizedWindows.delete('welcome');
                    saveMinimizedState();
                }
                return;
            }

            // Show welcome window (unless minimized)
            if (!minimizedWindows.has('welcome')) {
                welcomeWindow.style.display = '';
            }

            renderWelcomeSelector();
            updateUnreadBadge();

            // Auto-select first unread, or first message
            var firstUnread = welcomeMessages.find(function(m) { return !m.is_read && !m.is_muted; });
            if (firstUnread) {
                selectWelcomeMessage(firstUnread.id);
            } else if (welcomeMessages.length) {
                selectWelcomeMessage(welcomeMessages[0].id);
            }
        } catch (e) {
            console.error('Failed to load welcome messages:', e);
        }
    }

    function renderWelcomeSelector() {
        var layout = document.querySelector('.welcome-layout');
        var selector = document.getElementById('welcome-selector');
        if (!layout || !selector) return;

        // If only 1 message, hide selector
        if (welcomeMessages.length === 1) {
            layout.classList.add('single-message');
            selector.style.display = 'none';
            return;
        }

        layout.classList.remove('single-message');
        selector.style.display = '';

        // Sort: unread+unmuted first, then muted last
        var sorted = welcomeMessages.slice().sort(function(a, b) {
            var aUnread = !a.is_read && !a.is_muted ? 1 : 0;
            var bUnread = !b.is_read && !b.is_muted ? 1 : 0;
            if (bUnread !== aUnread) return bUnread - aUnread;
            if (a.is_muted !== b.is_muted) return a.is_muted ? 1 : -1;
            return 0;
        });

        selector.innerHTML = sorted.map(function(m) {
            var isUnread = !m.is_read && !m.is_muted;
            var classes = 'welcome-selector-item';
            if (isUnread) classes += ' unread';
            if (m.is_muted) classes += ' muted';

            var avatarContent = m.image_url
                ? '<img src="' + escapeHtml(m.image_url) + '" alt="">'
                : escapeHtml(m.initial || '?');

            return '<div class="' + classes + '" data-msg-id="' + m.id + '">'
                + '<div class="welcome-selector-avatar">' + avatarContent + '</div>'
                + '<div class="welcome-selector-info">'
                + '<div class="welcome-selector-name">' + escapeHtml(m.name) + '</div>'
                + '<div class="welcome-selector-creator">' + mh('explore.by_creator', {name: m.creator_name || ''}) + '</div>'
                + '</div>'
                + (isUnread ? '<div class="welcome-unread-dot"></div>' : '')
                + '</div>';
        }).join('');

        // Bind click events
        selector.querySelectorAll('.welcome-selector-item').forEach(function(el) {
            el.addEventListener('click', function() {
                var msgId = parseInt(el.getAttribute('data-msg-id'));
                selectWelcomeMessage(msgId);
            });
        });
    }

    function selectWelcomeMessage(id) {
        var msg = welcomeMessages.find(function(m) { return m.id === id; });
        if (!msg) return;

        // Clear any pending read timer
        Object.keys(readTimers).forEach(function(k) { clearTimeout(readTimers[k]); });

        // Highlight active in selector
        document.querySelectorAll('.welcome-selector-item').forEach(function(el) {
            el.classList.toggle('active', parseInt(el.getAttribute('data-msg-id')) === id);
        });

        var display = document.getElementById('welcome-display');
        if (!display) return;

        var avatarHtml = msg.image_url
            ? '<img src="' + escapeHtml(msg.image_url) + '" alt="">'
            : escapeHtml(msg.initial || '?');

        // Action buttons depend on entity_type
        var actionsHtml = '';
        if (msg.entity_type === 'prompt') {
            actionsHtml += '<button class="welcome-action-btn primary" data-action="chat" data-id="' + msg.entity_id + '">'
                + '<i class="fas fa-comment-dots"></i> ' + mh('home.start_chat') + '</button>';
        }
        if (msg.has_welcome_page) {
            actionsHtml += '<button class="welcome-action-btn secondary" data-action="welcome-page" data-type="' + msg.entity_type + '" data-id="' + msg.entity_id + '">'
                + '<i class="fas fa-globe"></i> ' + mh('home.welcome_page') + '</button>';
        }
        var muteLabel = msg.is_muted ? mt('home.unmute') : mt('home.mute');
        var muteIcon = msg.is_muted ? 'fa-bell' : 'fa-bell-slash';
        actionsHtml += '<button class="welcome-action-btn mute" data-action="mute-toggle" data-msg-id="' + msg.id + '" title="' + escapeHtml(muteLabel) + '">'
            + '<i class="fas ' + muteIcon + '"></i> ' + escapeHtml(muteLabel) + '</button>';

        display.innerHTML = '<div class="welcome-msg-header">'
            + '<div class="welcome-msg-avatar">' + avatarHtml + '</div>'
            + '<div class="welcome-msg-meta">'
            + '<div class="welcome-msg-name">' + escapeHtml(msg.name) + '</div>'
            + '<div class="welcome-msg-creator">' + mh('explore.by_creator', {name: msg.creator_name || ''}) + '</div>'
            + '</div></div>'
            + '<div class="welcome-msg-body">' + msg.content + '</div>'
            + '<div class="welcome-msg-actions">' + actionsHtml + '</div>';

        // Bind action buttons
        display.querySelectorAll('[data-action]').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var action = btn.getAttribute('data-action');
                if (action === 'chat') {
                    startChatWithPrompt(parseInt(btn.getAttribute('data-id')));
                } else if (action === 'welcome-page') {
                    navigateToWelcome(btn.getAttribute('data-type'), parseInt(btn.getAttribute('data-id')));
                } else if (action === 'mute-toggle') {
                    toggleMute(parseInt(btn.getAttribute('data-msg-id')));
                }
            });
        });

        // Mark as read after 3 seconds if unread
        if (!msg.is_read && !msg.is_muted) {
            readTimers[id] = setTimeout(function() { markAsRead(id); }, 3000);
        }
    }

    async function markAsRead(id) {
        var msg = welcomeMessages.find(function(m) { return m.id === id; });
        if (!msg || msg.is_read) return;

        try {
            var resp = await fetch('/api/home/welcome-messages/' + id + '/read', { method: 'PUT' });
            if (!resp.ok) return;
            msg.is_read = true;

            // Update selector item
            var item = document.querySelector('.welcome-selector-item[data-msg-id="' + id + '"]');
            if (item) {
                item.classList.remove('unread');
                var dot = item.querySelector('.welcome-unread-dot');
                if (dot) dot.remove();
            }
            updateUnreadBadge();
        } catch (e) {
            console.error('Failed to mark as read:', e);
        }
    }

    async function toggleMute(msgId) {
        var msg = welcomeMessages.find(function(m) { return m.id === msgId; });
        if (!msg) return;

        var action = msg.is_muted ? 'unmute' : 'mute';
        try {
            var resp = await fetch('/api/home/welcome-messages/' + msgId + '/' + action, { method: 'PUT' });
            if (!resp.ok) return;
            msg.is_muted = !msg.is_muted;

            renderWelcomeSelector();
            selectWelcomeMessage(msgId);
            updateUnreadBadge();
        } catch (e) {
            console.error('Failed to ' + action + ':', e);
        }
    }

    function updateUnreadBadge() {
        if (!welcomeMessages) return;
        var count = welcomeMessages.filter(function(m) { return !m.is_read && !m.is_muted; }).length;

        // Window header badge
        var badge = document.getElementById('welcome-badge');
        if (badge) {
            if (count > 0) {
                badge.textContent = AurvekI18n.formatNumber(count);
                badge.style.display = '';
            } else {
                badge.style.display = 'none';
            }
        }

        // Dock badge
        var dockBadge = document.querySelector('.dock-item[data-win="welcome"] .dock-item-badge');
        if (dockBadge) {
            if (count > 0) {
                dockBadge.textContent = AurvekI18n.formatNumber(count);
                dockBadge.style.display = '';
            } else {
                dockBadge.style.display = 'none';
            }
        }
    }

    // ==================== Dock System ====================

    function applyMinimizedState() {
        minimizedWindows.forEach(function(winId) {
            var winEl = document.getElementById('win-' + winId);
            if (winEl) {
                winEl.style.display = 'none';
            }
        });

        // Prune stale: if minimized but window element doesn't exist in DOM
        var pruned = false;
        minimizedWindows.forEach(function(winId) {
            var winEl = document.getElementById('win-' + winId);
            if (!winEl) {
                minimizedWindows.delete(winId);
                pruned = true;
            }
        });
        if (pruned) saveMinimizedState();

        renderDock();
        checkRowVisibility();
    }

    window.minimizeWindow = function(winId) {
        var winEl = document.getElementById('win-' + winId);
        if (!winEl) return;

        minimizedWindows.add(winId);
        winEl.classList.add('minimizing');
        setTimeout(function() {
            winEl.style.display = 'none';
            winEl.classList.remove('minimizing');
            renderDock();
            checkRowVisibility();
        }, 400);

        saveMinimizedState();
    };

    window.restoreWindow = function(winId) {
        var winEl = document.getElementById('win-' + winId);
        if (!winEl) return;

        minimizedWindows.delete(winId);
        winEl.style.display = '';
        winEl.classList.add('restoring');
        setTimeout(function() { winEl.classList.remove('restoring'); }, 450);

        renderDock();
        checkRowVisibility();
        saveMinimizedState();
    };

    function renderDock() {
        var dock = document.getElementById('dock');
        if (!dock) return;
        if (minimizedWindows.size === 0) {
            dock.classList.remove('visible');
            document.body.style.paddingBottom = '';
            return;
        }

        dock.classList.add('visible');
        document.body.style.paddingBottom = '80px';

        var items = [];

        if (minimizedWindows.has('welcome')) {
            var unread = welcomeMessages ? welcomeMessages.filter(function(m) { return !m.is_read && !m.is_muted; }).length : 0;
            items.push('<div class="dock-item" data-win="welcome" onclick="restoreWindow(\'welcome\')">'
                + '<div class="dock-item-icon welcome"><i class="fas fa-envelope-open-text"></i></div>'
                + '<span>' + mh('home.welcome') + '</span>'
                + (unread > 0 ? '<div class="dock-item-badge">' + escapeHtml(AurvekI18n.formatNumber(unread)) + '</div>' : '')
                + '</div>');
        }
        if (minimizedWindows.has('latest')) {
            items.push('<div class="dock-item" data-win="latest" onclick="restoreWindow(\'latest\')">'
                + '<div class="dock-item-icon latest"><i class="fas fa-bolt"></i></div>'
                + '<span>' + mh('home.latest') + '</span>'
                + '</div>');
        }
        if (minimizedWindows.has('library')) {
            items.push('<div class="dock-item" data-win="library" onclick="restoreWindow(\'library\')">'
                + '<div class="dock-item-icon library"><i class="fas fa-book-open"></i></div>'
                + '<span>' + mh('home.library_short') + '</span>'
                + '</div>');
        }

        dock.innerHTML = items.join('');
    }

    function checkRowVisibility() {
        var row = document.getElementById('windows-row');
        if (!row) return;
        var latestEl = document.getElementById('win-latest');
        var libraryEl = document.getElementById('win-library');
        var latestHidden = !latestEl || latestEl.style.display === 'none';
        var libraryHidden = !libraryEl || libraryEl.style.display === 'none';

        if (latestHidden && libraryHidden) {
            row.style.display = 'none';
        } else {
            row.style.display = '';
            row.style.gridTemplateColumns = (latestHidden || libraryHidden) ? '1fr' : '1fr 1fr';
        }
    }

    function saveMinimizedState() {
        var arr = Array.from(minimizedWindows).sort();
        fetch('/api/home/preferences', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ minimized_windows: arr })
        }).catch(function() {});
    }

    // ==================== Events ====================
    function bindEvents() {
        var input = document.getElementById('home-input');
        var sendBtn = document.getElementById('home-send-btn');

        function handleSend() {
            var text = input.value.trim();
            var select = document.getElementById('home-prompt-select');
            var selectedValue = select.value;

            if (!selectedValue) {
                select.focus();
                select.classList.add('home-prompt-select--highlight');
                setTimeout(function() { select.classList.remove('home-prompt-select--highlight'); }, 1500);
                return;
            }

            var promptId = selectedValue;
            if (selectedValue.startsWith('pack:')) {
                var packId = selectedValue.split(':')[1];
                navigateToWelcome('pack', parseInt(packId));
                return;
            }

            sessionStorage.setItem('home_start_prompt_id', promptId);
            if (text) {
                sessionStorage.setItem('home_start_message', text);
            }
            window.location.href = '/chat';
        }

        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSend();
            }
        });

        sendBtn.addEventListener('click', handleSend);

        // Favorite chips
        var favsContainer = document.getElementById('home-favorites');
        favsContainer.addEventListener('click', function(e) {
            var chip = e.target.closest('.home-fav-chip');
            if (!chip) return;
            var promptId = chip.getAttribute('data-prompt-id');
            var select = document.getElementById('home-prompt-select');
            for (var i = 0; i < select.options.length; i++) {
                if (select.options[i].value === promptId) {
                    select.selectedIndex = i;
                    break;
                }
            }
            favsContainer.querySelectorAll('.home-fav-chip').forEach(function(c) {
                c.classList.remove('active');
            });
            chip.classList.add('active');
            document.getElementById('home-input').focus();
        });

        // Tab toggles (library)
        var tabToggle = document.getElementById('home-tab-toggle');
        if (tabToggle) {
            tabToggle.addEventListener('click', function(e) {
                var btn = e.target.closest('.home-tab');
                if (!btn) return;
                var tab = btn.getAttribute('data-tab');
                tabToggle.querySelectorAll('.home-tab').forEach(function(t) { t.classList.remove('active'); });
                btn.classList.add('active');
                renderMyLibrary(tab);
            });
        }

        // View All button
        var viewAllBtn = document.getElementById('home-view-all-btn');
        if (viewAllBtn && tabToggle) {
            viewAllBtn.addEventListener('click', function(e) {
                e.preventDefault();
                var activeTab = tabToggle.querySelector('.home-tab.active');
                openViewAllModal(activeTab ? activeTab.getAttribute('data-tab') : 'prompts');
            });
        }

        // Modal
        var modalClose = document.getElementById('home-modal-close');
        if (modalClose) modalClose.addEventListener('click', closeModal);
        var modalOverlay = document.getElementById('home-modal-overlay');
        if (modalOverlay) {
            modalOverlay.addEventListener('click', function(e) {
                if (e.target === this) closeModal();
            });
        }
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') closeModal();
        });

        var modalTabs = document.getElementById('home-modal-tabs');
        if (modalTabs) {
            modalTabs.addEventListener('click', function(e) {
                var btn = e.target.closest('.home-tab');
                if (!btn) return;
                var tab = btn.getAttribute('data-tab');
                modalTabs.querySelectorAll('.home-tab').forEach(function(t) { t.classList.remove('active'); });
                btn.classList.add('active');
                var title = document.querySelector('.home-modal-title');
                if (title) title.textContent = (tab === 'packs') ? mt('home.all_packs') : mt('home.all_prompts');
                renderModalList(tab);
            });
        }

        // Window minimize buttons
        document.querySelectorAll('.window-minimize-btn').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var win = btn.closest('.window');
                if (win) {
                    var dockId = win.getAttribute('data-dock-id');
                    if (dockId) minimizeWindow(dockId);
                }
            });
        });
    }

    // ==================== Navigation ====================
    window.startChatWithPrompt = function(promptId) {
        sessionStorage.setItem('home_start_prompt_id', promptId);
        window.location.href = '/chat';
    };

    window.openChat = function(conversationId) {
        sessionStorage.setItem('home_open_conversation_id', conversationId);
        window.location.href = '/chat';
    };

    window.navigateToWelcome = function(type, id) {
        window.location.href = '/welcome/' + type + '/' + id;
    };

    window.toggleFavorite = async function(promptId, btnEl) {
        var isFav = btnEl.classList.contains('is-favorite');
        btnEl.classList.toggle('is-favorite');
        var icon = btnEl.querySelector('i');
        if (icon) {
            icon.className = isFav ? 'far fa-star' : 'fas fa-star';
        }
        try {
            var resp = await fetch('/api/home/favorites/' + promptId, { method: 'POST' });
            if (!resp.ok) throw new Error('Failed');
            var data = await resp.json();
            if (data.is_favorite) {
                if (!homeData.favorites.includes(promptId)) homeData.favorites.push(promptId);
            } else {
                homeData.favorites = homeData.favorites.filter(function(id) { return id !== promptId; });
            }
        } catch (e) {
            btnEl.classList.toggle('is-favorite');
            if (icon) icon.className = isFav ? 'fas fa-star' : 'far fa-star';
        }
    };

    // ==================== Utility ====================
    function escapeHtml(text) {
        if (!text) return '';
        var div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

})();
