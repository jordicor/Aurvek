/**
 * NekoGlass Controls - Glass Settings Panel for Neko Glass Theme
 * Self-contained module that injects wallpaper, floating buttons, and glass controls panel.
 * Only activates when theme === 'nekoglass'. Cleans up when switching away.
 */
const NekoGlassControls = (() => {
    'use strict';

    const STORAGE_KEY = 'nekoglass-settings';
    const displayOpacity = value => new Intl.NumberFormat(window.AurvekI18n.locale, {
        minimumFractionDigits: 2, maximumFractionDigits: 2
    }).format(value);
    const t = (key, params) => window.AurvekI18n.t(`navigation.${key}`, params);
    const escapeHtml = (value) => String(value)
        .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    const presetLabel = (label) => t(`nekoglass.preset.${label.toLowerCase().replaceAll(' ', '_')}`);

    // Default settings
    const DEFAULTS = {
        columnWidth: '64rem',
        columnWidthLabel: 'Medium',
        blurIntensity: 14,
        glassOpacity: 8,
        wallpaperDim: 13,
        accentIndex: 0,
        glassTintIndex: 0,
        glassTintCustom: null,
        userBubbleIndex: 0,
        userBubbleOpacity: 30,
        userBubbleCustom: null,
        userTextIndex: 0,
        userTextCustom: null,
        botBubbleIndex: 0,
        botBubbleCustom: null,
        botBubbleOpacity: 30,
        botTextIndex: 2,
        botTextCustom: null,
        wallpaperMode: 'default', // 'default' | 'custom'
        pageTextIndex: 0, // 0=Light, 1=Dark, 2=Custom
        pageTextCustom: null,
        fontSize: 14,
        bubbleRoundness: 1,
        expandedSections: []
    };

    // Accent color presets
    const ACCENT_PRESETS = [
        { h: 234, s: 85, l: 63, rgb: '80, 100, 240', label: 'Indigo' },
        { h: 270, s: 70, l: 60, rgb: '153, 82, 204', label: 'Purple' },
        { h: 180, s: 70, l: 48, rgb: '37, 196, 196', label: 'Teal' },
        { h: 340, s: 75, l: 58, rgb: '219, 68, 114', label: 'Pink' },
        { h: 145, s: 60, l: 45, rgb: '46, 184, 107', label: 'Green' }
    ];

    // Glass tint presets
    const GLASS_TINT_PRESETS = [
        { rgb: '255, 255, 255', label: 'White' },
        { rgb: '30, 30, 45', label: 'Charcoal' },
        { rgb: '15, 15, 40', label: 'Midnight' },
        { rgb: '20, 60, 80', label: 'Ocean' },
        { rgb: '50, 30, 70', label: 'Amethyst' },
        { rgb: '70, 30, 40', label: 'Rose' }
    ];

    // User bubble color presets
    const USER_BUBBLE_PRESETS = [
        { rgb: '80, 100, 240', label: 'Indigo' },
        { rgb: '140, 60, 200', label: 'Purple' },
        { rgb: '200, 60, 100', label: 'Rose' },
        { rgb: '40, 180, 180', label: 'Teal' },
        { rgb: '40, 170, 100', label: 'Emerald' },
        { rgb: '255, 160, 50', label: 'Amber' }
    ];

    // Bot bubble color presets
    const BOT_BUBBLE_PRESETS = [
        { rgb: '255, 255, 255', label: 'White Frost' },
        { rgb: '25, 25, 50', label: 'Dark Glass' },
        { rgb: '50, 50, 65', label: 'Smoke' },
        { rgb: '255, 180, 200', label: 'Blush' },
        { rgb: '180, 170, 255', label: 'Lavender' },
        { rgb: '170, 240, 210', label: 'Mint' }
    ];

    // User text color presets
    const USER_TEXT_PRESETS = [
        { color: '#ffffff', label: 'White' },
        { color: '#f0e8d8', label: 'Cream' },
        { color: '#2a2a3e', label: 'Dark' }
    ];

    // Bot text color presets (same order as user for consistency)
    const BOT_TEXT_PRESETS = [
        { color: '#ffffff', label: 'White' },
        { color: '#f0e8d8', label: 'Cream' },
        { color: '#2a2a3e', label: 'Dark' }
    ];

    // Width presets
    const WIDTH_PRESETS = [
        { value: '50rem', label: 'Narrow' },
        { value: '64rem', label: 'Medium' },
        { value: '80rem', label: 'Wide' },
        { value: '100rem', label: 'Wider' },
        { value: '92%', label: 'Full' }
    ];

    // Page text color presets (non-chat pages)
    const PAGE_TEXT_PRESETS = [
        { primary: '#e8eaed', secondary: '#c0c4cc', muted: '#8a8f9d', label: 'Light' },
        { primary: '#1a1b2e', secondary: '#2a2b3e', muted: '#4a4b5e', label: 'Dark' }
    ];

    // Bubble roundness presets
    const ROUNDNESS_PRESETS = [
        { value: '4px', label: 'Sharp' },
        { value: '12px', label: 'Soft' },
        { value: '20px', label: 'Round' },
        { value: '28px', label: 'Bubble' }
    ];

    let _initialized = false;
    let _settings = null;

    // Context: chat pages have #window-chat, non-chat pages don't
    let _isChatPage = false; // resolved in init() after DOM ready

    // --- Persistence ---
    function loadSettings() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (raw) {
                const parsed = Object.assign({}, DEFAULTS, JSON.parse(raw));
                // Ensure expandedSections is always an array
                if (!Array.isArray(parsed.expandedSections)) {
                    parsed.expandedSections = [];
                }
                return parsed;
            }
        } catch (e) {
            // Corrupted data - reset
        }
        return Object.assign({}, DEFAULTS, { expandedSections: [] });
    }

    function saveSettings() {
        if (!_settings) return;
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(_settings));
        } catch (e) {
            // Storage full or unavailable
        }
    }

    // --- CSS Custom Property Helpers ---
    function setCSSVar(name, value) {
        document.documentElement.style.setProperty(name, value);
    }

    function hexToRgb(hex) {
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        return `${r}, ${g}, ${b}`;
    }

    function applyAllSettings() {
        setCSSVar('--column-width', _settings.columnWidth);
        setCSSVar('--glass-blur', _settings.blurIntensity + 'px');
        setCSSVar('--glass-opacity', (_settings.glassOpacity / 100).toFixed(2));
        setCSSVar('--wallpaper-dim', (_settings.wallpaperDim / 100).toFixed(2));

        // Accent
        const accent = ACCENT_PRESETS[_settings.accentIndex] || ACCENT_PRESETS[0];
        setCSSVar('--accent-h', accent.h);
        setCSSVar('--accent-s', accent.s + '%');
        setCSSVar('--accent-l', accent.l + '%');
        setCSSVar('--accent-color-rgb', accent.rgb);

        // Glass tint
        if (_settings.glassTintIndex === 6 && _settings.glassTintCustom) {
            setCSSVar('--glass-tint-rgb', hexToRgb(_settings.glassTintCustom));
        } else {
            const tint = GLASS_TINT_PRESETS[_settings.glassTintIndex] || GLASS_TINT_PRESETS[0];
            setCSSVar('--glass-tint-rgb', tint.rgb);
        }

        // User bubble
        if (_settings.userBubbleIndex === 6 && _settings.userBubbleCustom) {
            setCSSVar('--user-bubble-rgb', hexToRgb(_settings.userBubbleCustom));
        } else {
            const userBubble = USER_BUBBLE_PRESETS[_settings.userBubbleIndex] || USER_BUBBLE_PRESETS[0];
            setCSSVar('--user-bubble-rgb', userBubble.rgb);
        }
        setCSSVar('--user-bubble-opacity', (_settings.userBubbleOpacity / 100).toFixed(2));

        // User text
        if (_settings.userTextIndex === 3 && _settings.userTextCustom) {
            setCSSVar('--user-text-color', _settings.userTextCustom);
        } else {
            const userText = USER_TEXT_PRESETS[_settings.userTextIndex] || USER_TEXT_PRESETS[0];
            setCSSVar('--user-text-color', userText.color);
        }

        // Bot bubble
        if (_settings.botBubbleIndex === 6 && _settings.botBubbleCustom) {
            setCSSVar('--bot-bubble-rgb', hexToRgb(_settings.botBubbleCustom));
        } else {
            const botBubble = BOT_BUBBLE_PRESETS[_settings.botBubbleIndex] || BOT_BUBBLE_PRESETS[0];
            setCSSVar('--bot-bubble-rgb', botBubble.rgb);
        }
        setCSSVar('--bot-bubble-opacity', (_settings.botBubbleOpacity / 100).toFixed(2));

        // Bot text
        if (_settings.botTextIndex === 3 && _settings.botTextCustom) {
            setCSSVar('--bot-text-color', _settings.botTextCustom);
        } else {
            const botText = BOT_TEXT_PRESETS[_settings.botTextIndex] || BOT_TEXT_PRESETS[0];
            setCSSVar('--bot-text-color', botText.color);
        }

        // Typography
        setCSSVar('--msg-font-size', _settings.fontSize + 'px');
        setCSSVar('--msg-border-radius', ROUNDNESS_PRESETS[_settings.bubbleRoundness]?.value || '12px');

        // Page text (non-chat pages only; chat has its own user/bot text controls)
        if (!_isChatPage && _settings.pageTextIndex > 0) {
            if (_settings.pageTextIndex === 2 && _settings.pageTextCustom) {
                const rgb = hexToRgb(_settings.pageTextCustom);
                setCSSVar('--text-primary', _settings.pageTextCustom);
                setCSSVar('--text-secondary', `rgba(${rgb}, 0.78)`);
                setCSSVar('--text-muted', `rgba(${rgb}, 0.58)`);
            } else {
                const pt = PAGE_TEXT_PRESETS[_settings.pageTextIndex] || PAGE_TEXT_PRESETS[0];
                setCSSVar('--text-primary', pt.primary);
                setCSSVar('--text-secondary', pt.secondary);
                setCSSVar('--text-muted', pt.muted);
            }
        }
    }

    // --- HTML Builders ---
    function buildCirclePresets(presets, activeIndex, idPrefix, customColor) {
        let html = presets.map((p, i) => {
            const rgb = p.rgb;
            const parts = rgb.split(',').map(n => parseInt(n.trim(), 10));
            const r = parts[0], g = parts[1], b = parts[2];
            const bgStyle = `background: linear-gradient(135deg, rgba(${r},${g},${b},0.9), rgba(${Math.max(0, r - 20)},${Math.max(0, g - 20)},${Math.max(0, b - 20)},1))`;
            return `<div class="preset-circle${i === activeIndex ? ' active' : ''}" data-index="${i}" style="${bgStyle}" title="${escapeHtml(presetLabel(p.label))}"></div>`;
        }).join('');
        // 7th circle: custom color picker
        const customStyle = (activeIndex === 6 && customColor)
            ? `background: ${customColor}`
            : '';
        html += `<div class="preset-circle custom-picker${activeIndex === 6 ? ' active' : ''}" data-index="6" title="${escapeHtml(t('nekoglass.custom'))}" ${customStyle ? `style="${customStyle}"` : ''}>
            <input type="color" class="custom-color-input" value="${customColor || '#888888'}">
        </div>`;
        return html;
    }

    function buildTextPills(presets, activeIndex, customColor) {
        let html = presets.map((p, i) =>
            `<button class="text-pill${i === activeIndex ? ' active' : ''}" data-index="${i}">${escapeHtml(presetLabel(p.label))}</button>`
        ).join('');
        // 4th pill: custom color picker
        const swatchColor = (activeIndex === 3 && customColor) ? customColor : '#888';
        html += `<button class="text-pill custom-text-pill${activeIndex === 3 ? ' active' : ''}" data-index="3">
            <span class="custom-text-swatch" style="background: ${swatchColor}"></span>
            ${escapeHtml(t('nekoglass.pick'))}
            <input type="color" class="custom-color-input" value="${customColor || '#888888'}">
        </button>`;
        return html;
    }

    function buildWidthPills() {
        return WIDTH_PRESETS.map(p =>
            `<button class="width-pill${p.value === _settings.columnWidth ? ' active' : ''}" data-width="${p.value}">${escapeHtml(presetLabel(p.label))}</button>`
        ).join('');
    }

    function buildRoundnessPills() {
        return ROUNDNESS_PRESETS.map((p, i) =>
            `<button class="width-pill${i === _settings.bubbleRoundness ? ' active' : ''}" data-index="${i}">${escapeHtml(presetLabel(p.label))}</button>`
        ).join('');
    }

    function buildAccentCircles() {
        return ACCENT_PRESETS.map((a, i) =>
            `<div class="preset-circle${i === _settings.accentIndex ? ' active' : ''}" data-index="${i}" style="background: linear-gradient(135deg, hsl(${a.h}, ${a.s}%, ${a.l}%), hsl(${a.h}, ${a.s}%, ${a.l - 10}%));" title="${escapeHtml(presetLabel(a.label))}"></div>`
        ).join('');
    }

    function isSectionExpanded(sectionId) {
        return Array.isArray(_settings.expandedSections) && _settings.expandedSections.includes(sectionId);
    }

    function buildSection(sectionId, icon, title, contentHtml) {
        const expanded = isSectionExpanded(sectionId);
        return `
            <div class="controls-section${expanded ? ' expanded' : ''}" data-section="${sectionId}">
                <div class="section-header">
                    <i class="${icon}"></i>
                    <span>${title}</span>
                    <i class="fas fa-chevron-down section-arrow"></i>
                </div>
                <div class="section-content">
                    ${contentHtml}
                </div>
            </div>`;
    }

    // --- DOM Injection ---
    function injectWallpaper() {
        if (document.querySelector('.neko-wallpaper')) return;

        const pageContent = document.getElementById('page-content');
        if (!pageContent) return;

        const wallpaper = document.createElement('div');
        wallpaper.className = 'neko-wallpaper';

        // Override CSS default wallpaper if user uploaded a custom one
        if (_settings && _settings.wallpaperMode === 'custom') {
            const customUrl = `/api/nekoglass/wallpaper?_=${Date.now()}`;
            wallpaper.style.backgroundImage = `url('${customUrl}')`;
            // Auto-revert if the server file was deleted externally
            const probe = new Image();
            probe.onerror = () => {
                _settings.wallpaperMode = 'default';
                saveSettings();
                wallpaper.style.backgroundImage = '';
            };
            probe.src = customUrl;
        }

        const overlay = document.createElement('div');
        overlay.className = 'neko-wallpaper-overlay';

        pageContent.parentNode.insertBefore(overlay, pageContent);
        pageContent.parentNode.insertBefore(wallpaper, overlay);
    }

    function injectFloatingButtons() {
        if (document.querySelector('.neko-floating-buttons')) return;

        const container = document.createElement('div');
        container.className = 'neko-floating-buttons';
        container.innerHTML = `
            <button class="neko-float-btn" id="neko-btn-glass-settings" title="${escapeHtml(t('nekoglass.settings'))}">
                <i class="fas fa-palette"></i>
            </button>
        `;

        document.body.appendChild(container);
        document.getElementById('neko-btn-glass-settings').addEventListener('click', togglePanel);
    }

    function injectControlsPanel() {
        if (document.querySelector('.glass-controls-panel')) return;

        const panel = document.createElement('div');
        panel.className = 'glass-controls-panel';
        panel.id = 'neko-glass-controls-panel';

        // Layout section content
        const pageTextCustomSwatch = (_settings.pageTextIndex === 2 && _settings.pageTextCustom) ? _settings.pageTextCustom : '#888';
        const layoutContent = `
            <div class="control-group">
                <div class="control-label">${escapeHtml(t('nekoglass.column_width'))}</div>
                <div class="width-pills" id="neko-width-pills">${buildWidthPills()}</div>
            </div>
            <div class="control-group" id="neko-pagetext-group">
                <div class="control-label">${escapeHtml(t('nekoglass.page_text'))}</div>
                <div class="width-pills" id="neko-pagetext-pills">
                    ${PAGE_TEXT_PRESETS.map((p, i) =>
                        `<button class="text-pill${i === _settings.pageTextIndex ? ' active' : ''}" data-index="${i}">${p.label}</button>`
                    ).join('')}
                    <button class="text-pill custom-text-pill${_settings.pageTextIndex === 2 ? ' active' : ''}" data-index="2">
                        <span class="custom-text-swatch" style="background: ${pageTextCustomSwatch}"></span>
                        ${escapeHtml(t('nekoglass.pick'))}
                        <input type="color" class="custom-color-input" value="${_settings.pageTextCustom || '#888888'}">
                    </button>
                </div>
            </div>
            <div class="control-group">
                <div class="control-label">
                    ${escapeHtml(t('nekoglass.font_size'))}
                    <span class="control-value" id="neko-fontsize-value">${_settings.fontSize}px</span>
                </div>
                <input type="range" class="control-slider" id="neko-fontsize-slider" min="11" max="20" value="${_settings.fontSize}" step="1">
            </div>
            <div class="control-group">
                <div class="control-label">${escapeHtml(t('nekoglass.bubble_roundness'))}</div>
                <div class="width-pills" id="neko-roundness-pills">${buildRoundnessPills()}</div>
            </div>`;

        // Glass effect section content
        const isCustomWp = _settings.wallpaperMode === 'custom';
        const glassContent = `
            <div class="control-group neko-wallpaper-control">
                <div class="control-label">${escapeHtml(t('nekoglass.wallpaper'))}</div>
                <div class="wallpaper-actions">
                    <label class="wallpaper-upload-btn" id="neko-wallpaper-upload-label">
                        <i class="fas fa-image"></i> ${escapeHtml(isCustomWp ? t('nekoglass.change') : t('nekoglass.upload'))}
                        <input type="file" id="neko-wallpaper-input" accept="image/jpeg,image/png,image/webp,image/gif" hidden>
                    </label>
                    <button class="wallpaper-reset-btn" id="neko-wallpaper-reset" ${isCustomWp ? '' : 'disabled'}>
                        <i class="fas fa-undo"></i> ${escapeHtml(t('nekoglass.default'))}
                    </button>
                </div>
                <div class="wallpaper-status" id="neko-wallpaper-status">${isCustomWp ? escapeHtml(t('nekoglass.wallpaper_active')) : ''}</div>
            </div>
            <div class="control-group">
                <div class="control-label">${escapeHtml(t('nekoglass.glass_tint'))}</div>
                <div class="preset-circles" id="neko-glasstint-circles">${buildCirclePresets(GLASS_TINT_PRESETS, _settings.glassTintIndex, 'glasstint', _settings.glassTintCustom)}</div>
            </div>
            <div class="control-group">
                <div class="control-label">
                    ${escapeHtml(t('nekoglass.blur_intensity'))}
                    <span class="control-value" id="neko-blur-value">${_settings.blurIntensity}px</span>
                </div>
                <input type="range" class="control-slider" id="neko-blur-slider" min="0" max="30" value="${_settings.blurIntensity}" step="1">
            </div>
            <div class="control-group">
                <div class="control-label">
                    ${escapeHtml(t('nekoglass.glass_opacity'))}
                    <span class="control-value" id="neko-opacity-value">${displayOpacity(_settings.glassOpacity / 100)}</span>
                </div>
                <input type="range" class="control-slider" id="neko-opacity-slider" min="0" max="25" value="${_settings.glassOpacity}" step="1">
            </div>
            <div class="control-group">
                <div class="control-label">
                    ${escapeHtml(t('nekoglass.wallpaper_dim'))}
                    <span class="control-value" id="neko-dim-value">${displayOpacity(_settings.wallpaperDim / 100)}</span>
                </div>
                <input type="range" class="control-slider" id="neko-dim-slider" min="0" max="50" value="${_settings.wallpaperDim}" step="1">
            </div>`;

        // User messages section content
        const userMsgContent = `
            <div class="control-group">
                <div class="control-label">${escapeHtml(t('nekoglass.bubble_color'))}</div>
                <div class="preset-circles" id="neko-userbubble-circles">${buildCirclePresets(USER_BUBBLE_PRESETS, _settings.userBubbleIndex, 'userbubble', _settings.userBubbleCustom)}</div>
            </div>
            <div class="control-group">
                <div class="control-label">
                    ${escapeHtml(t('nekoglass.bubble_opacity'))}
                    <span class="control-value" id="neko-useropacity-value">${displayOpacity(_settings.userBubbleOpacity / 100)}</span>
                </div>
                <input type="range" class="control-slider" id="neko-useropacity-slider" min="5" max="60" value="${_settings.userBubbleOpacity}" step="1">
            </div>
            <div class="control-group">
                <div class="control-label">${escapeHtml(t('nekoglass.text_color'))}</div>
                <div class="width-pills" id="neko-usertext-pills">${buildTextPills(USER_TEXT_PRESETS, _settings.userTextIndex, _settings.userTextCustom)}</div>
            </div>`;

        // Bot messages section content
        const botMsgContent = `
            <div class="control-group">
                <div class="control-label">${escapeHtml(t('nekoglass.bubble_color'))}</div>
                <div class="preset-circles" id="neko-botbubble-circles">${buildCirclePresets(BOT_BUBBLE_PRESETS, _settings.botBubbleIndex, 'botbubble', _settings.botBubbleCustom)}</div>
            </div>
            <div class="control-group">
                <div class="control-label">
                    ${escapeHtml(t('nekoglass.bubble_opacity'))}
                    <span class="control-value" id="neko-botopacity-value">${displayOpacity(_settings.botBubbleOpacity / 100)}</span>
                </div>
                <input type="range" class="control-slider" id="neko-botopacity-slider" min="5" max="60" value="${_settings.botBubbleOpacity}" step="1">
            </div>
            <div class="control-group">
                <div class="control-label">${escapeHtml(t('nekoglass.text_color'))}</div>
                <div class="width-pills" id="neko-bottext-pills">${buildTextPills(BOT_TEXT_PRESETS, _settings.botTextIndex, _settings.botTextCustom)}</div>
            </div>`;

        // UI accent section content (affects buttons, sidebar highlights, active states)
        const accentContent = `
            <div class="control-group">
                <div class="control-label">${escapeHtml(t('nekoglass.interface_color'))}</div>
                <div class="preset-circles" id="neko-accent-circles">${buildAccentCircles()}</div>
            </div>`;

        panel.innerHTML = `
            <div class="controls-header">
                <div class="controls-title">
                    <i class="fas fa-wand-magic-sparkles"></i>
                    ${escapeHtml(t('nekoglass.settings'))}
                </div>
            </div>

            <div class="controls-body">
                ${buildSection('layout', 'fas fa-columns', escapeHtml(t('nekoglass.layout')), layoutContent)}
                ${buildSection('glass', 'fas fa-droplet', escapeHtml(t('nekoglass.glass_effect')), glassContent)}
                ${buildSection('user-msg', 'fas fa-comment', escapeHtml(t('nekoglass.user_messages')), userMsgContent)}
                ${buildSection('bot-msg', 'fas fa-robot', escapeHtml(t('nekoglass.bot_messages')), botMsgContent)}
                ${buildSection('accent', 'fas fa-swatchbook', escapeHtml(t('nekoglass.ui_accent')), accentContent)}
            </div>

            <div class="controls-footer">
                <button class="reset-btn" id="neko-reset-btn">
                    <i class="fas fa-rotate-left"></i>
                    ${escapeHtml(t('nekoglass.reset_all'))}
                </button>
            </div>
        `;

        document.body.appendChild(panel);

        // Hide chat-only sections on non-chat pages
        if (!_isChatPage) {
            panel.querySelector('[data-section="user-msg"]').style.display = 'none';
            panel.querySelector('[data-section="bot-msg"]').style.display = 'none';
            // Hide Font Size and Bubble Roundness within Layout section
            const fontGroup = panel.querySelector('#neko-fontsize-slider')?.closest('.control-group');
            if (fontGroup) fontGroup.style.display = 'none';
            const roundGroup = panel.querySelector('#neko-roundness-pills')?.closest('.control-group');
            if (roundGroup) roundGroup.style.display = 'none';
        } else {
            // Hide Page Text on chat pages (chat has User/Bot Message text controls)
            const pageTextGroup = panel.querySelector('#neko-pagetext-group');
            if (pageTextGroup) pageTextGroup.style.display = 'none';
        }

        bindControlEvents(panel);
    }

    // --- Event Binding ---
    function bindControlEvents(panel) {
        // Section toggle (accordion)
        panel.querySelectorAll('.section-header').forEach(header => {
            header.addEventListener('click', () => {
                const section = header.closest('.controls-section');
                const sectionId = section.dataset.section;
                const isExpanded = section.classList.toggle('expanded');

                if (isExpanded) {
                    if (!_settings.expandedSections.includes(sectionId)) {
                        _settings.expandedSections.push(sectionId);
                    }
                } else {
                    _settings.expandedSections = _settings.expandedSections.filter(s => s !== sectionId);
                }
                saveSettings();
            });
        });

        // Width pills
        panel.querySelector('#neko-width-pills').addEventListener('click', (e) => {
            const pill = e.target.closest('.width-pill');
            if (!pill) return;
            panel.querySelectorAll('#neko-width-pills .width-pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            _settings.columnWidth = pill.dataset.width;
            _settings.columnWidthLabel = pill.textContent;
            setCSSVar('--column-width', _settings.columnWidth);
            saveSettings();
        });

        // Page text pills + custom picker
        const pageTextContainer = panel.querySelector('#neko-pagetext-pills');
        if (pageTextContainer) {
            pageTextContainer.addEventListener('click', (e) => {
                const pill = e.target.closest('.text-pill');
                if (!pill) return;
                const idx = parseInt(pill.dataset.index, 10);
                if (idx === 2) {
                    pill.querySelector('.custom-color-input').click();
                    return;
                }
                pageTextContainer.querySelectorAll('.text-pill').forEach(p => p.classList.remove('active'));
                pill.classList.add('active');
                _settings.pageTextIndex = idx;
                if (idx === 0) {
                    // Light: remove overrides, CSS defaults take over
                    ['--text-primary', '--text-secondary', '--text-muted'].forEach(v =>
                        document.documentElement.style.removeProperty(v));
                } else {
                    const pt = PAGE_TEXT_PRESETS[idx];
                    setCSSVar('--text-primary', pt.primary);
                    setCSSVar('--text-secondary', pt.secondary);
                    setCSSVar('--text-muted', pt.muted);
                }
                saveSettings();
            });
            const pageTextCustomInput = pageTextContainer.querySelector('.custom-text-pill .custom-color-input');
            if (pageTextCustomInput) {
                pageTextCustomInput.addEventListener('input', (e) => {
                    const color = e.target.value;
                    _settings.pageTextIndex = 2;
                    _settings.pageTextCustom = color;
                    pageTextContainer.querySelectorAll('.text-pill').forEach(p => p.classList.remove('active'));
                    const customPill = pageTextContainer.querySelector('[data-index="2"]');
                    customPill.classList.add('active');
                    customPill.querySelector('.custom-text-swatch').style.background = color;
                    const rgb = hexToRgb(color);
                    setCSSVar('--text-primary', color);
                    setCSSVar('--text-secondary', `rgba(${rgb}, 0.78)`);
                    setCSSVar('--text-muted', `rgba(${rgb}, 0.58)`);
                    saveSettings();
                });
            }
        }

        // Font size slider
        const fontSizeSlider = panel.querySelector('#neko-fontsize-slider');
        fontSizeSlider.addEventListener('input', () => {
            _settings.fontSize = parseInt(fontSizeSlider.value, 10);
            setCSSVar('--msg-font-size', _settings.fontSize + 'px');
            panel.querySelector('#neko-fontsize-value').textContent = _settings.fontSize + 'px';
            saveSettings();
        });

        // Roundness pills
        panel.querySelector('#neko-roundness-pills').addEventListener('click', (e) => {
            const pill = e.target.closest('.width-pill');
            if (!pill) return;
            panel.querySelectorAll('#neko-roundness-pills .width-pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            _settings.bubbleRoundness = parseInt(pill.dataset.index, 10);
            setCSSVar('--msg-border-radius', ROUNDNESS_PRESETS[_settings.bubbleRoundness]?.value || '12px');
            saveSettings();
        });

        // Blur slider
        const blurSlider = panel.querySelector('#neko-blur-slider');
        blurSlider.addEventListener('input', () => {
            _settings.blurIntensity = parseInt(blurSlider.value, 10);
            setCSSVar('--glass-blur', _settings.blurIntensity + 'px');
            panel.querySelector('#neko-blur-value').textContent = _settings.blurIntensity + 'px';
            saveSettings();
        });

        // Opacity slider
        const opacitySlider = panel.querySelector('#neko-opacity-slider');
        opacitySlider.addEventListener('input', () => {
            _settings.glassOpacity = parseInt(opacitySlider.value, 10);
            const val = (_settings.glassOpacity / 100).toFixed(2);
            setCSSVar('--glass-opacity', val);
            panel.querySelector('#neko-opacity-value').textContent = displayOpacity(Number(val));
            saveSettings();
        });

        // Dim slider
        const dimSlider = panel.querySelector('#neko-dim-slider');
        dimSlider.addEventListener('input', () => {
            _settings.wallpaperDim = parseInt(dimSlider.value, 10);
            const val = (_settings.wallpaperDim / 100).toFixed(2);
            setCSSVar('--wallpaper-dim', val);
            panel.querySelector('#neko-dim-value').textContent = displayOpacity(Number(val));
            saveSettings();
        });

        // Wallpaper upload
        const wpInput = panel.querySelector('#neko-wallpaper-input');
        const wpStatus = panel.querySelector('#neko-wallpaper-status');
        const wpResetBtn = panel.querySelector('#neko-wallpaper-reset');
        const wpUploadLabel = panel.querySelector('#neko-wallpaper-upload-label');

        wpInput.addEventListener('change', async () => {
            const file = wpInput.files[0];
            if (!file) return;

            if (file.size > 5 * 1024 * 1024) {
                wpStatus.textContent = t('nekoglass.error.too_large');
                wpStatus.classList.add('error');
                wpInput.value = '';
                return;
            }

            // Client-side dimension check to avoid server timeouts on huge images
            wpStatus.textContent = t('nekoglass.checking_image');
            wpStatus.classList.remove('error');
            try {
                await new Promise((resolve, reject) => {
                    const url = URL.createObjectURL(file);
                    const img = new Image();
                    img.onload = () => {
                        URL.revokeObjectURL(url);
                        const pixels = img.width * img.height;
                        if (pixels > 50_000_000) {
                            reject(new Error(
                                t('nekoglass.error.resolution', { width: img.width, height: img.height })
                            ));
                        } else {
                            resolve();
                        }
                    };
                    img.onerror = () => {
                        URL.revokeObjectURL(url);
                        reject(new Error(t('nekoglass.error.cannot_read')));
                    };
                    img.src = url;
                });
            } catch (validationErr) {
                wpStatus.textContent = validationErr.message;
                wpStatus.classList.add('error');
                wpInput.value = '';
                return;
            }

            wpStatus.textContent = t('nekoglass.uploading');

            const formData = new FormData();
            formData.append('file', file);

            try {
                const res = await fetch('/api/nekoglass/wallpaper', {
                    method: 'POST',
                    body: formData,
                    credentials: 'include'
                });

                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    let msg = err.detail;
                    if (!msg) {
                        switch (res.status) {
                            case 400: msg = t('nekoglass.error.invalid_image'); break;
                            case 401: msg = t('nekoglass.error.session_expired'); break;
                            case 413: msg = t('nekoglass.error.file_too_large'); break;
                            case 524: msg = t('nekoglass.error.timeout'); break;
                            case 502:
                            case 503: msg = t('nekoglass.error.server_unavailable'); break;
                            default:  msg = t('nekoglass.error.upload_failed', { status: res.status });
                        }
                    }
                    throw new Error(msg);
                }

                _settings.wallpaperMode = 'custom';
                saveSettings();

                // Update wallpaper live
                const wp = document.querySelector('.neko-wallpaper');
                if (wp) wp.style.backgroundImage = `url('/api/nekoglass/wallpaper?_=${Date.now()}')`;

                wpStatus.textContent = t('nekoglass.wallpaper_active');
                wpStatus.classList.remove('error');
                wpResetBtn.disabled = false;
                wpUploadLabel.childNodes.forEach(n => {
                    if (n.nodeType === 3 && n.textContent.trim()) n.textContent = ` ${t('nekoglass.change')}`;
                });
            } catch (e) {
                wpStatus.textContent = e.message;
                wpStatus.classList.add('error');
            }

            // Reset input so the same file can be re-selected
            wpInput.value = '';
        });

        wpResetBtn.addEventListener('click', async () => {
            wpStatus.textContent = t('nekoglass.resetting');
            wpStatus.classList.remove('error');

            try {
                await fetch('/api/nekoglass/wallpaper', {
                    method: 'DELETE',
                    credentials: 'include'
                });

                _settings.wallpaperMode = 'default';
                saveSettings();

                // Revert to CSS default wallpaper
                const wp = document.querySelector('.neko-wallpaper');
                if (wp) wp.style.backgroundImage = '';

                wpStatus.textContent = '';
                wpResetBtn.disabled = true;
                wpUploadLabel.childNodes.forEach(n => {
                    if (n.nodeType === 3 && n.textContent.trim()) n.textContent = ` ${t('nekoglass.upload')}`;
                });
            } catch (e) {
                wpStatus.textContent = t('nekoglass.error.reset_failed');
                wpStatus.classList.add('error');
            }
        });

        // Glass tint circles + custom picker
        const glassTintContainer = panel.querySelector('#neko-glasstint-circles');
        glassTintContainer.addEventListener('click', (e) => {
            const circle = e.target.closest('.preset-circle');
            if (!circle) return;
            const idx = parseInt(circle.dataset.index, 10);
            if (idx === 6) {
                // Custom picker - trigger color input
                circle.querySelector('.custom-color-input').click();
                return;
            }
            glassTintContainer.querySelectorAll('.preset-circle').forEach(c => c.classList.remove('active'));
            circle.classList.add('active');
            _settings.glassTintIndex = idx;
            setCSSVar('--glass-tint-rgb', GLASS_TINT_PRESETS[idx].rgb);
            saveSettings();
        });
        glassTintContainer.querySelector('.custom-picker .custom-color-input').addEventListener('input', (e) => {
            const color = e.target.value;
            _settings.glassTintIndex = 6;
            _settings.glassTintCustom = color;
            glassTintContainer.querySelectorAll('.preset-circle').forEach(c => c.classList.remove('active'));
            const customCircle = glassTintContainer.querySelector('[data-index="6"]');
            customCircle.classList.add('active');
            customCircle.style.background = color;
            setCSSVar('--glass-tint-rgb', hexToRgb(color));
            saveSettings();
        });

        // User bubble circles + custom picker
        const userBubbleContainer = panel.querySelector('#neko-userbubble-circles');
        userBubbleContainer.addEventListener('click', (e) => {
            const circle = e.target.closest('.preset-circle');
            if (!circle) return;
            const idx = parseInt(circle.dataset.index, 10);
            if (idx === 6) {
                circle.querySelector('.custom-color-input').click();
                return;
            }
            userBubbleContainer.querySelectorAll('.preset-circle').forEach(c => c.classList.remove('active'));
            circle.classList.add('active');
            _settings.userBubbleIndex = idx;
            setCSSVar('--user-bubble-rgb', USER_BUBBLE_PRESETS[idx].rgb);
            saveSettings();
        });
        userBubbleContainer.querySelector('.custom-picker .custom-color-input').addEventListener('input', (e) => {
            const color = e.target.value;
            _settings.userBubbleIndex = 6;
            _settings.userBubbleCustom = color;
            userBubbleContainer.querySelectorAll('.preset-circle').forEach(c => c.classList.remove('active'));
            const customCircle = userBubbleContainer.querySelector('[data-index="6"]');
            customCircle.classList.add('active');
            customCircle.style.background = color;
            setCSSVar('--user-bubble-rgb', hexToRgb(color));
            saveSettings();
        });

        // User bubble opacity slider
        const userOpacitySlider = panel.querySelector('#neko-useropacity-slider');
        userOpacitySlider.addEventListener('input', () => {
            _settings.userBubbleOpacity = parseInt(userOpacitySlider.value, 10);
            const val = (_settings.userBubbleOpacity / 100).toFixed(2);
            setCSSVar('--user-bubble-opacity', val);
            panel.querySelector('#neko-useropacity-value').textContent = displayOpacity(Number(val));
            saveSettings();
        });

        // User text pills + custom picker
        const userTextContainer = panel.querySelector('#neko-usertext-pills');
        userTextContainer.addEventListener('click', (e) => {
            const pill = e.target.closest('.text-pill');
            if (!pill) return;
            const idx = parseInt(pill.dataset.index, 10);
            if (idx === 3) {
                pill.querySelector('.custom-color-input').click();
                return;
            }
            userTextContainer.querySelectorAll('.text-pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            _settings.userTextIndex = idx;
            setCSSVar('--user-text-color', USER_TEXT_PRESETS[idx].color);
            saveSettings();
        });
        userTextContainer.querySelector('.custom-text-pill .custom-color-input').addEventListener('input', (e) => {
            const color = e.target.value;
            _settings.userTextIndex = 3;
            _settings.userTextCustom = color;
            userTextContainer.querySelectorAll('.text-pill').forEach(p => p.classList.remove('active'));
            const customPill = userTextContainer.querySelector('[data-index="3"]');
            customPill.classList.add('active');
            customPill.querySelector('.custom-text-swatch').style.background = color;
            setCSSVar('--user-text-color', color);
            saveSettings();
        });

        // Bot bubble circles + custom picker
        const botBubbleContainer = panel.querySelector('#neko-botbubble-circles');
        botBubbleContainer.addEventListener('click', (e) => {
            const circle = e.target.closest('.preset-circle');
            if (!circle) return;
            const idx = parseInt(circle.dataset.index, 10);
            if (idx === 6) {
                circle.querySelector('.custom-color-input').click();
                return;
            }
            botBubbleContainer.querySelectorAll('.preset-circle').forEach(c => c.classList.remove('active'));
            circle.classList.add('active');
            _settings.botBubbleIndex = idx;
            setCSSVar('--bot-bubble-rgb', BOT_BUBBLE_PRESETS[idx].rgb);
            saveSettings();
        });
        botBubbleContainer.querySelector('.custom-picker .custom-color-input').addEventListener('input', (e) => {
            const color = e.target.value;
            _settings.botBubbleIndex = 6;
            _settings.botBubbleCustom = color;
            botBubbleContainer.querySelectorAll('.preset-circle').forEach(c => c.classList.remove('active'));
            const customCircle = botBubbleContainer.querySelector('[data-index="6"]');
            customCircle.classList.add('active');
            customCircle.style.background = color;
            setCSSVar('--bot-bubble-rgb', hexToRgb(color));
            saveSettings();
        });

        // Bot bubble opacity slider
        const botOpacitySlider = panel.querySelector('#neko-botopacity-slider');
        botOpacitySlider.addEventListener('input', () => {
            _settings.botBubbleOpacity = parseInt(botOpacitySlider.value, 10);
            const val = (_settings.botBubbleOpacity / 100).toFixed(2);
            setCSSVar('--bot-bubble-opacity', val);
            panel.querySelector('#neko-botopacity-value').textContent = displayOpacity(Number(val));
            saveSettings();
        });

        // Bot text pills + custom picker
        const botTextContainer = panel.querySelector('#neko-bottext-pills');
        botTextContainer.addEventListener('click', (e) => {
            const pill = e.target.closest('.text-pill');
            if (!pill) return;
            const idx = parseInt(pill.dataset.index, 10);
            if (idx === 3) {
                pill.querySelector('.custom-color-input').click();
                return;
            }
            botTextContainer.querySelectorAll('.text-pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            _settings.botTextIndex = idx;
            setCSSVar('--bot-text-color', BOT_TEXT_PRESETS[idx].color);
            saveSettings();
        });
        botTextContainer.querySelector('.custom-text-pill .custom-color-input').addEventListener('input', (e) => {
            const color = e.target.value;
            _settings.botTextIndex = 3;
            _settings.botTextCustom = color;
            botTextContainer.querySelectorAll('.text-pill').forEach(p => p.classList.remove('active'));
            const customPill = botTextContainer.querySelector('[data-index="3"]');
            customPill.classList.add('active');
            customPill.querySelector('.custom-text-swatch').style.background = color;
            setCSSVar('--bot-text-color', color);
            saveSettings();
        });

        // Accent circles
        panel.querySelector('#neko-accent-circles').addEventListener('click', (e) => {
            const circle = e.target.closest('.preset-circle');
            if (!circle) return;
            panel.querySelectorAll('#neko-accent-circles .preset-circle').forEach(c => c.classList.remove('active'));
            circle.classList.add('active');
            _settings.accentIndex = parseInt(circle.dataset.index, 10);
            const accent = ACCENT_PRESETS[_settings.accentIndex];
            setCSSVar('--accent-h', accent.h);
            setCSSVar('--accent-s', accent.s + '%');
            setCSSVar('--accent-l', accent.l + '%');
            setCSSVar('--accent-color-rgb', accent.rgb);
            saveSettings();
        });

        // Reset button
        panel.querySelector('#neko-reset-btn').addEventListener('click', async () => {
            // Delete custom wallpaper if present
            if (_settings.wallpaperMode === 'custom') {
                fetch('/api/nekoglass/wallpaper', { method: 'DELETE', credentials: 'include' }).catch(() => {});
            }

            _settings = Object.assign({}, DEFAULTS);
            _settings.expandedSections = [];
            saveSettings();
            applyAllSettings();

            // Revert wallpaper to CSS default
            const wp = document.querySelector('.neko-wallpaper');
            if (wp) wp.style.backgroundImage = '';

            // Revert page text to CSS defaults
            ['--text-primary', '--text-secondary', '--text-muted'].forEach(v =>
                document.documentElement.style.removeProperty(v));

            // Re-render panel by removing and re-injecting
            const existingPanel = document.getElementById('neko-glass-controls-panel');
            const wasOpen = existingPanel?.classList.contains('open');
            existingPanel?.remove();
            injectControlsPanel();
            if (wasOpen) {
                document.getElementById('neko-glass-controls-panel')?.classList.add('open');
            }
        });

        // Click outside to close
        document.addEventListener('click', _onDocumentClick);
    }

    function _onDocumentClick(e) {
        const panel = document.getElementById('neko-glass-controls-panel');
        const btn = document.getElementById('neko-btn-glass-settings');
        if (panel && btn && !panel.contains(e.target) && !btn.contains(e.target) && panel.classList.contains('open')) {
            panel.classList.remove('open');
            btn.classList.remove('active');
        }
    }

    // --- Panel Toggle ---
    function togglePanel() {
        const panel = document.getElementById('neko-glass-controls-panel');
        const btn = document.getElementById('neko-btn-glass-settings');
        if (!panel || !btn) return;

        const isOpen = panel.classList.toggle('open');
        btn.classList.toggle('active', isOpen);
    }

    // --- Cleanup ---
    function cleanup() {
        // Remove injected DOM
        document.querySelectorAll('.neko-wallpaper, .neko-wallpaper-overlay, .neko-floating-buttons, .glass-controls-panel').forEach(el => el.remove());
        // Remove event listener
        document.removeEventListener('click', _onDocumentClick);
        // Reset CSS vars to defaults
        ['--column-width', '--glass-blur', '--glass-opacity', '--wallpaper-dim',
         '--accent-h', '--accent-s', '--accent-l', '--accent-color-rgb',
         '--glass-tint-rgb', '--user-bubble-rgb', '--user-bubble-opacity', '--user-text-color',
         '--bot-bubble-rgb', '--bot-bubble-opacity', '--bot-text-color',
         '--msg-font-size', '--msg-border-radius',
         '--text-primary', '--text-secondary', '--text-muted'].forEach(v => {
            document.documentElement.style.removeProperty(v);
        });
        _initialized = false;
    }

    // --- Sidebar Menu Positioning Fix ---
    // CSS .chat-menu uses flexbox centering (no transform) so position:fixed
    // on .chat-menu-content escapes overflow clipping from .list-group.
    // Glass effect is on #sidebar::before so no containing block is created.
    function resetFixedMenu(m) {
        m.style.position = '';
        m.style.zIndex = '';
        m.style.right = '';
        m.style.left = '';
        m.style.top = '';
        m.style.bottom = '';
    }

    function patchSidebarMenus() {
        const sidebar = document.getElementById('sidebar');
        if (!sidebar) return;

        // Make user dropdown open upward (disable Popper so CSS controls it)
        const userDropdownTrigger = document.getElementById('userDropdownDesktop');
        if (userDropdownTrigger) {
            userDropdownTrigger.setAttribute('data-bs-display', 'static');
        }

        // Use capture phase to intercept after original handler shows the menu
        sidebar.addEventListener('click', (e) => {
            const chatMenu = e.target.closest('.chat-menu');
            if (!chatMenu) return;

            // Defer to let the original handler show the menu first
            requestAnimationFrame(() => {
                const content = chatMenu.querySelector('.chat-menu-content');
                if (!content || content.style.display !== 'block') return;

                // Use the icon as position reference (chatMenu spans full item height)
                const icon = chatMenu.querySelector('i') || chatMenu;
                const iconRect = icon.getBoundingClientRect();

                content.style.position = 'fixed';
                content.style.zIndex = '99999';
                content.style.right = (window.innerWidth - iconRect.right) + 'px';
                content.style.left = 'auto';

                const menuHeight = content.offsetHeight;
                if (iconRect.bottom + menuHeight > window.innerHeight) {
                    content.style.top = 'auto';
                    content.style.bottom = (window.innerHeight - iconRect.top + 4) + 'px';
                } else {
                    content.style.top = (iconRect.bottom + 4) + 'px';
                    content.style.bottom = 'auto';
                }
            });
        }, true);

        // Clean up fixed positioning when menus are closed by original handlers
        document.addEventListener('click', () => {
            requestAnimationFrame(() => {
                sidebar.querySelectorAll('.chat-menu-content').forEach(m => {
                    if (m.style.display !== 'block' && m.style.position === 'fixed') {
                        resetFixedMenu(m);
                    }
                });
            });
        });

        // Close fixed menus on any scroll inside sidebar (capture catches all descendants)
        sidebar.addEventListener('scroll', () => {
            sidebar.querySelectorAll('.chat-menu-content').forEach(m => {
                if (m.style.position === 'fixed') {
                    m.style.display = 'none';
                    resetFixedMenu(m);
                    const parentItem = m.closest('.list-group-item');
                    if (parentItem) parentItem.style.zIndex = '';
                }
            });
        }, true);
    }

    // --- Public API ---
    function init() {
        // Self-gate on theme
        const currentTheme = localStorage.getItem('theme');
        if (currentTheme !== 'nekoglass') {
            cleanup();
            return;
        }

        if (_initialized) return;
        _initialized = true;

        _isChatPage = !!document.getElementById('window-chat');

        _settings = loadSettings();
        applyAllSettings();
        injectWallpaper();
        injectFloatingButtons();
        injectControlsPanel();
        patchSidebarMenus();
    }

    // Listen for theme changes to clean up
    document.addEventListener('theme:changed', (e) => {
        if (e.detail && e.detail.theme !== 'nekoglass') {
            cleanup();
        }
    });

    return { init };
})();
