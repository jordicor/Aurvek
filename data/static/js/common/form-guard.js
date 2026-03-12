/**
 * FormGuard — Unsaved Changes Protection
 * Detects unsaved form changes and warns users before navigation.
 *
 * Modes:
 *   Snapshot  — serializes form state, compares on navigation
 *   Listener  — tracks input/change events (for dynamic containers)
 *   CodeMirror — hooks into CM change event
 *
 * Usage:
 *   <form data-form-guard>           // auto-discovery (server-rendered only)
 *   FormGuard.watch(form, opts)      // snapshot mode
 *   FormGuard.watchWithListeners(el) // listener mode
 *   FormGuard.createGroup(name)      // guard group for complex pages
 *   FormGuard.navigate(url, opts)    // safe programmatic navigation
 *   FormGuard.reloadIfClean()        // safe reload after AJAX save
 */
(function() {
    'use strict';

    var _ungrouped = [];
    var _groups = [];
    var _guardDisabled = false;
    var _guardModalOpen = false;

    // -- Helpers --

    function resolveEl(elOrSelector) {
        if (typeof elOrSelector === 'string') return document.querySelector(elOrSelector);
        return elOrSelector;
    }

    // -- Serialization (snapshot mode) --

    function serializeForm(form, excludeNames) {
        var data = {};
        var excluded = new Set(excludeNames || []);
        var elements = form.querySelectorAll('input, textarea, select');

        for (var i = 0; i < elements.length; i++) {
            var el = elements[i];
            if (excluded.has(el.name)) continue;
            if (!el.name || el.disabled) continue;

            if (el.type === 'checkbox') {
                var siblings = form.querySelectorAll(
                    'input[type="checkbox"][name="' + CSS.escape(el.name) + '"]'
                );
                if (siblings.length > 1) {
                    if (!(el.name in data)) {
                        data[el.name] = [];
                        siblings.forEach(function(cb) { if (cb.checked && !cb.disabled) data[el.name].push(cb.value); });
                        data[el.name].sort();
                    }
                } else {
                    data[el.name] = el.checked;
                }
            } else if (el.type === 'radio') {
                if (el.checked) data[el.name] = el.value;
                else if (!(el.name in data)) data[el.name] = null;
            } else if (el.type === 'file') {
                data[el.name] = Array.from(el.files).map(function(f) { return f.name; }).join(',');
            } else if (el.tagName === 'SELECT' && el.multiple) {
                data[el.name] = Array.from(el.selectedOptions).map(function(o) { return o.value; }).sort();
            } else {
                if (el.name in data) {
                    var n = 2;
                    while ((el.name + '__' + n) in data) n++;
                    data[el.name + '__' + n] = el.value;
                } else {
                    data[el.name] = el.value;
                }
            }
        }
        return JSON.stringify(data, Object.keys(data).sort());
    }

    // -- Dirty check for ungrouped entries --

    function isEntryDirty(entry) {
        if (entry._fgDirtyManual) return true;
        if (entry._fgMode === 'snapshot') {
            return serializeForm(entry._fgElement, entry._fgExclude) !== entry._fgSnapshot;
        }
        return entry._fgDirty;
    }

    // -- shouldWarn — single decision point --

    function shouldWarn() {
        if (_guardDisabled) return false;
        for (var i = 0; i < _groups.length; i++) {
            if (_groups[i].suspended) continue;
            if (_groups[i].isDirty()) return true;
        }
        for (var j = 0; j < _ungrouped.length; j++) {
            if (_ungrouped[j]._fgSubmitting) continue;
            if (isEntryDirty(_ungrouped[j])) return true;
        }
        return false;
    }

    // -- Listener watcher setup --

    function setupListenerWatcher(entry) {
        var container = entry._fgElement;
        function onDirty() { entry._fgDirty = true; }
        container.addEventListener('input', onDirty);
        container.addEventListener('change', onDirty);
        entry._fgCleanupListeners = function() {
            container.removeEventListener('input', onDirty);
            container.removeEventListener('change', onDirty);
        };
    }

    // -- Warning modal --

    function showWarningModal(onDiscard) {
        if (_guardModalOpen) return;
        _guardModalOpen = true;
        NotificationModal.confirm(
            'Unsaved Changes',
            'You have unsaved changes that will be lost if you leave this page.',
            function() { onDiscard(); },
            function() { _guardModalOpen = false; },
            { confirmText: 'Discard Changes', cancelText: 'Stay on Page', type: 'warning' }
        );
    }

    // -- Watcher handle (used by groups) --

    function createHandle(element, mode, opts) {
        var handle = {
            _fgElement: element,
            _fgMode: mode,
            _fgDirty: false,
            _fgDirtyManual: false,
            _fgExclude: (opts && opts.exclude) || [],
            _fgSnapshot: null,
            _fgCleanupListeners: null
        };

        if (mode === 'snapshot') {
            handle._fgSnapshot = serializeForm(element, handle._fgExclude);
        } else if (mode === 'listener') {
            setupListenerWatcher(handle);
        }

        handle.markClean = function() {
            handle._fgDirty = false;
            handle._fgDirtyManual = false;
            if (mode === 'snapshot') {
                handle._fgSnapshot = serializeForm(element, handle._fgExclude);
            }
        };
        handle.markDirty = function() { handle._fgDirtyManual = true; };
        handle.isDirty = function() {
            if (handle._fgDirtyManual) return true;
            if (mode === 'snapshot') return serializeForm(element, handle._fgExclude) !== handle._fgSnapshot;
            return handle._fgDirty;
        };

        return handle;
    }

    // -- Guard Group --

    function GuardGroup(name) {
        this.name = name;
        this.suspended = false;
        this._watchers = [];
    }

    GuardGroup.prototype.isDirty = function() {
        for (var i = 0; i < this._watchers.length; i++) {
            var w = this._watchers[i];
            if (w.isDirty()) return true;
        }
        return false;
    };

    GuardGroup.prototype.markClean = function() {
        for (var i = 0; i < this._watchers.length; i++) {
            this._watchers[i].markClean();
        }
    };

    GuardGroup.prototype.suspend = function() { this.suspended = true; };
    GuardGroup.prototype.resume = function() { this.suspended = false; };

    GuardGroup.prototype.destroy = function() {
        for (var i = 0; i < this._watchers.length; i++) {
            if (this._watchers[i]._fgCleanupListeners) {
                this._watchers[i]._fgCleanupListeners();
            }
        }
        this._watchers = [];
        var idx = _groups.indexOf(this);
        if (idx !== -1) _groups.splice(idx, 1);
    };

    GuardGroup.prototype.watchSnapshot = function(formOrSelector, opts) {
        var form = resolveEl(formOrSelector);
        if (!form) return null;
        var handle = createHandle(form, 'snapshot', opts);
        this._watchers.push(handle);
        return handle;
    };

    GuardGroup.prototype.watchWithListeners = function(containerOrSelector) {
        var container = resolveEl(containerOrSelector);
        if (!container) return null;
        var handle = createHandle(container, 'listener');
        this._watchers.push(handle);
        return handle;
    };

    GuardGroup.prototype.watchCodeMirror = function(containerOrSelector, cmInstance) {
        var container = resolveEl(containerOrSelector);
        if (!container) return null;
        var handle = createHandle(container, 'codemirror');
        if (cmInstance && cmInstance.on) {
            var onCMChange = function() { handle._fgDirty = true; };
            cmInstance.on('change', onCMChange);
            handle._fgCleanupListeners = function() { cmInstance.off('change', onCMChange); };
        }
        this._watchers.push(handle);
        return handle;
    };

    // -- FormGuard Public API --

    window.FormGuard = {

        watch: function(formOrSelector, opts) {
            var form = resolveEl(formOrSelector);
            if (!form) return null;
            var exclude = (opts && opts.exclude) || [];
            var entry = {
                _fgElement: form,
                _fgMode: 'snapshot',
                _fgExclude: exclude,
                _fgSnapshot: serializeForm(form, exclude),
                _fgDirty: false,
                _fgDirtyManual: false,
                _fgSubmitting: false
            };
            _ungrouped.push(entry);

            // Auto-attach submit handler for form POST
            form.addEventListener('submit', function() {
                entry._fgSubmitting = true;
            });

            return entry;
        },

        watchWithListeners: function(containerOrSelector) {
            var container = resolveEl(containerOrSelector);
            if (!container) return null;
            var entry = {
                _fgElement: container,
                _fgMode: 'listener',
                _fgDirty: false,
                _fgDirtyManual: false,
                _fgSubmitting: false
            };
            setupListenerWatcher(entry);
            _ungrouped.push(entry);
            return entry;
        },

        watchCodeMirror: function(containerOrSelector, cmInstance) {
            var container = resolveEl(containerOrSelector);
            if (!container) return null;
            var entry = {
                _fgElement: container,
                _fgMode: 'codemirror',
                _fgDirty: false,
                _fgDirtyManual: false,
                _fgSubmitting: false
            };
            if (cmInstance && cmInstance.on) {
                var onCMChange = function() { entry._fgDirty = true; };
                cmInstance.on('change', onCMChange);
                entry._fgCleanupListeners = function() { cmInstance.off('change', onCMChange); };
            }
            _ungrouped.push(entry);
            return entry;
        },

        markDirty: function(elOrSelector) {
            var el = resolveEl(elOrSelector);
            if (!el) return;
            for (var i = 0; i < _ungrouped.length; i++) {
                if (_ungrouped[i]._fgElement === el) {
                    _ungrouped[i]._fgDirtyManual = true;
                    return;
                }
            }
        },

        markClean: function(elOrSelector) {
            var el = resolveEl(elOrSelector);
            if (!el) return;
            for (var i = 0; i < _ungrouped.length; i++) {
                if (_ungrouped[i]._fgElement === el) {
                    var entry = _ungrouped[i];
                    entry._fgDirty = false;
                    entry._fgDirtyManual = false;
                    entry._fgSubmitting = false;
                    if (entry._fgMode === 'snapshot') {
                        entry._fgSnapshot = serializeForm(entry._fgElement, entry._fgExclude);
                    }
                    return;
                }
            }
        },

        isDirty: function(elOrSelector) {
            var el = resolveEl(elOrSelector);
            if (!el) return false;
            for (var i = 0; i < _ungrouped.length; i++) {
                if (_ungrouped[i]._fgElement === el) return isEntryDirty(_ungrouped[i]);
            }
            return false;
        },

        unwatch: function(elOrSelector) {
            var el = resolveEl(elOrSelector);
            if (!el) return;
            for (var i = 0; i < _ungrouped.length; i++) {
                if (_ungrouped[i]._fgElement === el) {
                    if (_ungrouped[i]._fgCleanupListeners) _ungrouped[i]._fgCleanupListeners();
                    _ungrouped.splice(i, 1);
                    return;
                }
            }
        },

        createGroup: function(name) {
            var group = new GuardGroup(name);
            _groups.push(group);
            return group;
        },

        getGroup: function(name) {
            for (var i = 0; i < _groups.length; i++) {
                if (_groups[i].name === name) return _groups[i];
            }
            return null;
        },

        navigate: function(url, opts) {
            if (opts && opts.bypass) {
                _guardDisabled = true;
                window.location.href = url;
                setTimeout(function() { _guardDisabled = false; }, 3000);
                return;
            }
            if (!shouldWarn()) {
                window.location.href = url;
                return;
            }
            showWarningModal(function() {
                FormGuard.navigate(url, { bypass: true });
            });
        },

        reloadIfClean: function() {
            if (!shouldWarn()) {
                window.location.reload();
                return;
            }
            NotificationModal.confirm(
                'Unsaved Changes',
                'Other sections on this page have unsaved changes that will be lost if the page reloads.',
                function() { FormGuard.navigate(location.href, { bypass: true }); },
                null,
                { confirmText: 'Discard Changes', cancelText: 'Stay on Page', type: 'warning' }
            );
        },

        anyDirty: function() {
            return shouldWarn();
        }
    };

    // -- Link click interceptor (document-level, bubbling) --

    document.addEventListener('click', function(e) {
        if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;

        var link = e.target.closest('a[href]');
        if (!link) return;

        var href = link.getAttribute('href');
        if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
        if (href.startsWith('mailto:') || href.startsWith('tel:')) return;
        if (link.hasAttribute('download')) return;
        if (link.hasAttribute('data-no-guard')) return;
        if (link.target === '_blank') return;

        if (!shouldWarn()) return;
        if (_guardModalOpen) return;

        e.preventDefault();
        e.stopImmediatePropagation();
        _guardModalOpen = true;

        NotificationModal.confirm(
            'Unsaved Changes',
            'You have unsaved changes that will be lost if you leave this page.',
            function() { FormGuard.navigate(link.href, { bypass: true }); },
            function() { _guardModalOpen = false; },
            { confirmText: 'Discard Changes', cancelText: 'Stay on Page', type: 'warning' }
        );
    });

    // Reset modal flag on any dismiss path (X, backdrop, Escape)
    document.addEventListener('hidden.bs.modal', function(e) {
        if (e.target.id === 'notificationModal') _guardModalOpen = false;
    });

    // -- beforeunload --

    window.addEventListener('beforeunload', function(e) {
        if (shouldWarn()) {
            e.preventDefault();
            e.returnValue = '';
        }
    });

    // -- Auto-discovery (Tier 1 only, server-rendered pages) --

    document.addEventListener('DOMContentLoaded', function() {
        requestAnimationFrame(function() {
            document.querySelectorAll('form[data-form-guard]').forEach(function(form) {
                FormGuard.watch(form);
            });
        });
    });

})();
