/* Change native chat identity without reloading or losing the draft. */
(function(root) {
    'use strict';

    const tr = key => root.AurvekI18n.t(`navigation.${key}`);
    let identities = [];
    let selectedId = null;
    let loading = null;
    let saving = false;

    function menus() {
        return document.querySelectorAll('[data-submenu="alter-egos"]');
    }

    function notify(key) {
        NotificationModal.error(tr('menu.alter_egos'), tr(key));
    }

    function render() {
        menus().forEach(menu => {
            menu.replaceChildren();
            identities.forEach(identity => {
                const item = document.createElement('li');
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'dropdown-item d-flex align-items-center gap-2';
                button.textContent = identity.id === null ? tr('alter_ego.real_profile') : identity.name;
                button.style.whiteSpace = 'normal';
                button.style.overflowWrap = 'anywhere';
                button.disabled = saving;
                const active = identity.id === selectedId;
                button.classList.toggle('active', active);
                button.setAttribute('aria-current', String(active));
                const check = document.createElement('span');
                check.className = 'ms-auto';
                check.textContent = active ? '\u2713' : '';
                check.setAttribute('aria-hidden', 'true');
                button.appendChild(check);
                button.addEventListener('click', event => {
                    event.preventDefault();
                    event.stopPropagation();
                    void select(identity);
                });
                item.appendChild(button);
                menu.appendChild(item);
            });
        });
    }

    async function load() {
        if (loading) return loading;
        const focusMenu = document.activeElement?.classList.contains('alter-ego-toggle');
        loading = (async () => {
            try {
                const response = await root.fetch('/api/get-alter-egos', { credentials: 'same-origin' });
                const data = await response.json();
                if (!response.ok || !data.success) throw new Error('Unable to load identities');
                identities = [data.profile, ...data.alterEgos];
                selectedId = data.currentAlterEgoId;
                render();
                if (focusMenu) {
                    document.querySelector('[data-submenu="alter-egos"] .active')?.focus();
                }
            } catch (_error) {
                menus().forEach(menu => {
                    menu.replaceChildren();
                    const item = document.createElement('li');
                    const retry = document.createElement('button');
                    retry.type = 'button';
                    retry.className = 'dropdown-item';
                    retry.textContent = tr('alter_ego.load_failed');
                    retry.addEventListener('click', event => {
                        event.stopPropagation();
                        void load();
                    });
                    item.appendChild(retry);
                    menu.appendChild(item);
                });
            } finally {
                loading = null;
            }
        })();
        return loading;
    }

    async function select(identity) {
        if (saving || identity.id === selectedId) return;
        if (document.getElementById('send-button')?.dataset.streaming === 'true'
            || root.WellbeingVoiceActive === true
            || document.getElementById('phone-active-card')?.hidden === false) {
            notify('alter_ego.finish_activity');
            return;
        }
        saving = true;
        menus().forEach(menu => menu.querySelectorAll('button').forEach(button => { button.disabled = true; }));
        try {
            const response = await root.fetch('/api/active-alter-ego', {
                method: 'POST', credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-GPTSub-CSRF': document.querySelector('meta[name="aurvek-csrf-token"]').content,
                },
                body: JSON.stringify({ alter_ego_id: identity.id }),
            });
            const data = await response.json();
            if (!response.ok || !data.success || data.currentAlterEgoId !== identity.id) {
                throw new Error('Unable to save identity');
            }
            selectedId = identity.id;
            document.querySelectorAll('.sidebar-user .user-info-name').forEach(name => {
                name.textContent = identity.name;
            });
            document.querySelectorAll('.sidebar-user .user-avatar').forEach(avatar => {
                avatar.replaceChildren();
                if (identity.profilePicture) {
                    const image = document.createElement('img');
                    image.src = identity.profilePicture;
                    image.alt = '';
                    image.className = 'user-avatar-img';
                    avatar.appendChild(image);
                } else {
                    avatar.textContent = identity.name.charAt(0).toUpperCase();
                }
            });
            root.userProfilePicture = identity.profilePicture || '';
            root.username = identity.name;
        } catch (_error) {
            notify('alter_ego.change_failed');
        } finally {
            saving = false;
            render();
        }
    }

    function init() {
        menus().forEach(menu => {
            // Fetch on demand; opening a menu later refreshes changes from Settings.
            menu.style.maxWidth = 'min(320px, calc(100vw - 16px))';
            menu.style.maxHeight = 'min(420px, 65vh)';
            menu.style.overflowY = 'auto';
            menu.addEventListener('settings-submenu:show', () => { if (!saving) void load(); });
        });
        document.getElementById('userDropdownDesktop')?.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                event.currentTarget.click();
            }
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})(window);
