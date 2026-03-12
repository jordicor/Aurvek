// admin-packs.js - Pack create/edit page logic

(function() {
    'use strict';

    const packId = document.getElementById('packId')?.value || '';
    const isEdit = packId !== '';

    // -----------------------------------------------------------------------
    // Slug auto-generation
    // -----------------------------------------------------------------------

    const nameInput = document.getElementById('packName');
    const slugInput = document.getElementById('packSlug');
    let slugManuallyEdited = isEdit;

    if (nameInput && slugInput) {
        nameInput.addEventListener('blur', function() {
            if (!slugManuallyEdited || !slugInput.value.trim()) {
                slugInput.value = slugify(nameInput.value);
            }
        });
        slugInput.addEventListener('input', function() {
            slugManuallyEdited = true;
        });
    }

    function slugify(text) {
        return text.toString().toLowerCase().trim()
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .replace(/[^a-z0-9\s-]/g, '')
            .replace(/[\s_]+/g, '-')
            .replace(/-+/g, '-')
            .replace(/^-|-$/g, '');
    }

    // -----------------------------------------------------------------------
    // Price toggle
    // -----------------------------------------------------------------------

    const isPaidCheckbox = document.getElementById('packIsPaid');
    const priceRow = document.getElementById('priceRow');

    function togglePrice() {
        if (isPaidCheckbox && priceRow) {
            if (isPaidCheckbox.checked) {
                priceRow.classList.add('visible');
            } else {
                priceRow.classList.remove('visible');
            }
        }
    }

    // -----------------------------------------------------------------------
    // Balance cap hint
    // -----------------------------------------------------------------------

    const packPriceInput = document.getElementById('packPrice');
    const balanceHint = document.getElementById('lrcBalanceHint');

    function updateBalanceHint() {
        if (!balanceHint) return;
        var maxBal;
        if (isPaidCheckbox && isPaidCheckbox.checked && packPriceInput) {
            var price = parseFloat(packPriceInput.value) || 0;
            if (price > 0 && typeof COMMISSION_RATE !== 'undefined') {
                maxBal = Math.round(price * (1 - COMMISSION_RATE) * 100) / 100;
            } else {
                maxBal = typeof MAX_FREE_BALANCE !== 'undefined' ? MAX_FREE_BALANCE : 5.0;
            }
        } else {
            maxBal = typeof MAX_FREE_BALANCE !== 'undefined' ? MAX_FREE_BALANCE : 5.0;
        }
        balanceHint.textContent = 'Max: $' + maxBal.toFixed(2);
    }

    if (isPaidCheckbox) {
        isPaidCheckbox.addEventListener('change', togglePrice);
        isPaidCheckbox.addEventListener('change', updateBalanceHint);
        togglePrice();
    }

    if (packPriceInput) {
        packPriceInput.addEventListener('input', updateBalanceHint);
    }

    updateBalanceHint();

    // -----------------------------------------------------------------------
    // Tags
    // -----------------------------------------------------------------------

    let tags = [];
    const tagsContainer = document.getElementById('tagsContainer');
    const tagInput = document.getElementById('tagInput');
    const addTagBtn = document.getElementById('addTagBtn');

    function initTags() {
        // Parse existing tags from pack data
        const packDataEl = document.getElementById('packId');
        if (!packDataEl) return;

        // Try to get tags from the page (embedded in a hidden input or via template)
        const tagsEl = document.getElementById('packTagsData');
        const tagsRaw = tagsEl ? tagsEl.value : '[]';
        try {
            const parsed = JSON.parse(tagsRaw);
            if (Array.isArray(parsed)) {
                tags = parsed;
            }
        } catch (e) {
            tags = [];
        }
        renderTags();
    }

    function renderTags() {
        if (!tagsContainer) return;
        tagsContainer.innerHTML = '';
        tags.forEach(function(tag, idx) {
            const chip = document.createElement('span');
            chip.className = 'tag-chip';
            chip.textContent = tag + ' ';
            var removeBtn = document.createElement('span');
            removeBtn.className = 'tag-remove';
            removeBtn.dataset.idx = idx;
            removeBtn.innerHTML = '&times;';
            chip.appendChild(removeBtn);
            tagsContainer.appendChild(chip);
        });

        // Attach remove handlers
        tagsContainer.querySelectorAll('.tag-remove').forEach(function(el) {
            el.addEventListener('click', function() {
                const i = parseInt(this.getAttribute('data-idx'));
                tags.splice(i, 1);
                renderTags();
            });
        });
    }

    function addTag() {
        if (!tagInput) return;
        const val = tagInput.value.trim().substring(0, 30);
        if (!val) return;
        if (tags.length >= 10) {
            NotificationModal.toast('Maximum 10 tags allowed', 'warning');
            return;
        }
        if (tags.indexOf(val) !== -1) {
            NotificationModal.toast('Tag already exists', 'warning');
            return;
        }
        tags.push(val);
        tagInput.value = '';
        renderTags();
    }

    if (addTagBtn) {
        addTagBtn.addEventListener('click', addTag);
    }
    if (tagInput) {
        tagInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                addTag();
            }
        });
    }

    initTags();

    // -----------------------------------------------------------------------
    // Landing reg config helper
    // -----------------------------------------------------------------------

    function getLandingRegConfig() {
        return {
            initial_balance: parseFloat(document.getElementById('lrcBalance')?.value || '5.0'),
            billing_mode: document.getElementById('lrcBilling')?.value || 'user_pays',
            public_prompts_access: document.getElementById('lrcPublicPrompts')?.checked || false,
            allow_file_upload: document.getElementById('lrcFileUpload')?.checked || false,
            allow_image_generation: document.getElementById('lrcImageGen')?.checked || false,
        };
    }

    // -----------------------------------------------------------------------
    // Form submit (Save Draft)
    // -----------------------------------------------------------------------

    const form = document.getElementById('packForm');
    if (form) {
        form.addEventListener('submit', async function(e) {
            e.preventDefault();
            const btn = document.getElementById('saveDraftBtn');
            const originalHTML = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Saving...';

            const data = {
                name: document.getElementById('packName').value.trim(),
                slug: document.getElementById('packSlug').value.trim(),
                description: document.getElementById('packDescription').value.trim(),
                is_paid: document.getElementById('packIsPaid')?.checked || false,
                price: parseFloat(document.getElementById('packPrice')?.value || '0'),
                tags: tags,
                landing_reg_config: getLandingRegConfig(),
            };

            try {
                const url = isEdit ? '/api/packs/' + packId : '/api/packs';
                const method = isEdit ? 'PUT' : 'POST';

                const response = await fetch(url, {
                    method: method,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data),
                });
                const result = await response.json();

                if (response.ok) {
                    // Upload cover image if a file is pending
                    var savedPackId = isEdit ? packId : result.id;
                    var coverUploaded = false;
                    if (hasPendingCoverImage() && savedPackId) {
                        try {
                            await uploadCoverImage(savedPackId);
                            coverUploaded = true;
                        } catch (imgError) {
                            NotificationModal.toast('Pack saved but cover image failed: ' + imgError.message, 'warning');
                        }
                    }

                    btn.innerHTML = '<i class="fas fa-check me-1"></i> Saved!';
                    btn.classList.add('btn-success');
                    NotificationModal.toast(result.message || 'Pack saved', 'success');

                    // FormGuard: mark groups clean after successful save
                    if (typeof packMainGroup !== 'undefined') packMainGroup.markClean();
                    if (typeof packTagsGroup !== 'undefined') packTagsGroup.markClean();
                    if (typeof packCoverGroup !== 'undefined') {
                        packCoverGroup._manualDirty = false;
                        packCoverGroup.markClean();
                    }

                    if (!isEdit && result.id) {
                        // Redirect to edit page after creation
                        if (typeof FormGuard !== 'undefined') {
                            FormGuard.navigate('/admin/packs/edit/' + result.id, { bypass: true });
                        } else {
                            window.location.href = '/admin/packs/edit/' + result.id;
                        }
                        return;
                    }

                    // Reload to show the new cover image if one was uploaded
                    if (coverUploaded) {
                        setTimeout(function() { FormGuard.reloadIfClean(); }, 1000);
                        return;
                    }

                    setTimeout(function() {
                        btn.innerHTML = originalHTML;
                        btn.classList.remove('btn-success');
                        btn.disabled = false;
                    }, 2000);
                } else {
                    NotificationModal.toast(result.detail || 'Failed to save pack', 'danger');
                    btn.innerHTML = originalHTML;
                    btn.disabled = false;
                }
            } catch (error) {
                NotificationModal.toast('Error: ' + error.message, 'danger');
                btn.innerHTML = originalHTML;
                btn.disabled = false;
            }
        });
    }

    // -----------------------------------------------------------------------
    // Publish / Unpublish
    // -----------------------------------------------------------------------

    window.publishPack = function() {
        if (!isEdit) return;
        NotificationModal.confirm('Publish Pack', 'Publish this pack? It will be visible to all users.', async () => {
            try {
                const response = await fetch('/api/packs/' + packId + '/publish', { method: 'POST' });
                const result = await response.json();
                if (response.ok) {
                    NotificationModal.toast('Pack published!', 'success');
                    setTimeout(function() { location.reload(); }, 1000);
                } else {
                    if (result.detail && typeof result.detail === 'object') {
                        var msg = result.detail.message || 'Failed to publish';
                        var reason = result.detail.reason ? ': ' + result.detail.reason : '';
                        NotificationModal.toast(msg + reason, 'danger');
                    } else {
                        NotificationModal.toast(result.detail || 'Failed to publish', 'danger');
                    }
                }
            } catch (error) {
                NotificationModal.toast('Error: ' + error.message, 'danger');
            }
        }, null, { type: 'warning', confirmText: 'Publish' });
    };

    window.unpublishPack = function() {
        if (!isEdit) return;
        NotificationModal.confirm('Unpublish Pack', 'Unpublish this pack? It will no longer be visible to users.', async () => {
            try {
                const response = await fetch('/api/packs/' + packId + '/unpublish', { method: 'POST' });
                const result = await response.json();
                if (response.ok) {
                    NotificationModal.toast('Pack unpublished', 'success');
                    setTimeout(function() { location.reload(); }, 1000);
                } else {
                    NotificationModal.toast(result.detail || 'Failed to unpublish', 'danger');
                }
            } catch (error) {
                NotificationModal.toast('Error: ' + error.message, 'danger');
            }
        }, null, { type: 'warning', confirmText: 'Unpublish' });
    };

    // -----------------------------------------------------------------------
    // Pack items: Remove
    // -----------------------------------------------------------------------

    window.removePackItem = function(promptId, promptName) {
        if (!isEdit) return;
        NotificationModal.confirm('Remove Prompt', 'Remove "' + promptName + '" from this pack?', async () => {
            try {
                const response = await fetch('/api/packs/' + packId + '/items/' + promptId, { method: 'DELETE' });
                const result = await response.json();
                if (response.ok) {
                    // Remove row from table
                    const row = document.querySelector('#packItemsBody tr[data-prompt-id="' + promptId + '"]');
                    if (row) row.remove();
                    renumberItems();
                    if (typeof packItemsGroup !== 'undefined') packItemsGroup.markClean();
                    NotificationModal.toast('Prompt removed', 'success');
                } else {
                    NotificationModal.toast(result.detail || 'Failed to remove prompt', 'danger');
                }
            } catch (error) {
                NotificationModal.toast('Error: ' + error.message, 'danger');
            }
        }, null, { type: 'error', confirmText: 'Remove' });
    };

    function renumberItems() {
        document.querySelectorAll('#packItemsBody tr .item-order').forEach(function(td, i) {
            td.textContent = i + 1;
        });
    }

    // -----------------------------------------------------------------------
    // Pack items: Drag-and-drop reorder
    // -----------------------------------------------------------------------

    const itemsBody = document.getElementById('packItemsBody');
    if (itemsBody && typeof Sortable !== 'undefined') {
        Sortable.create(itemsBody, {
            handle: '.drag-handle',
            animation: 150,
            onEnd: async function() {
                renumberItems();
                // Save new order
                const orderedIds = [];
                itemsBody.querySelectorAll('tr[data-prompt-id]').forEach(function(row) {
                    orderedIds.push(parseInt(row.getAttribute('data-prompt-id')));
                });
                if (orderedIds.length === 0) return;

                try {
                    await fetch('/api/packs/' + packId + '/items/reorder', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ prompt_ids: orderedIds }),
                    });
                } catch (e) {
                    // Silent fail on reorder - next page load will show saved order
                }
            }
        });
    }

    // -----------------------------------------------------------------------
    // Pack items: Add Prompt search modal
    // -----------------------------------------------------------------------

    const searchInput = document.getElementById('promptSearchInput');
    const resultsContainer = document.getElementById('addPromptResults');
    const emptyMsg = document.getElementById('promptSearchEmpty');
    let searchTimeout = null;

    if (searchInput) {
        searchInput.addEventListener('input', function() {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(function() { searchPrompts(searchInput.value); }, 300);
        });

        // Load initial results when modal opens
        const modal = document.getElementById('addPromptModal');
        if (modal) {
            modal.addEventListener('shown.bs.modal', function() {
                searchInput.value = '';
                searchInput.focus();
                searchPrompts('');
            });
        }
    }

    async function searchPrompts(query) {
        if (!resultsContainer) return;
        try {
            const response = await fetch('/api/packs/' + packId + '/available-prompts?search=' + encodeURIComponent(query));
            const prompts = await response.json();

            resultsContainer.innerHTML = '';
            if (emptyMsg) emptyMsg.style.display = prompts.length === 0 ? 'block' : 'none';

            prompts.forEach(function(p) {
                const div = document.createElement('div');
                div.className = 'prompt-search-item';
                div.setAttribute('data-prompt-id', p.id);

                let avatarHtml;
                if (p.image) {
                    avatarHtml = '<img src="/static/' + p.image + '" alt="">';
                } else {
                    avatarHtml = '<div class="prompt-search-placeholder"><i class="fas fa-robot"></i></div>';
                }

                const notice = p.pack_notice_period_days > 0
                    ? (p.pack_notice_period_days >= 365
                        ? Math.floor(p.pack_notice_period_days / 365) + 'y'
                        : p.pack_notice_period_days + 'd')
                    : '';

                div.innerHTML = avatarHtml +
                    '<div style="flex:1;min-width:0;">' +
                        '<div style="font-weight:500;">' + escapeHtml(p.name) + '</div>' +
                        '<small class="text-muted">' + escapeHtml(p.owner_username || '') +
                        (notice ? ' &middot; Notice: ' + notice : '') + '</small>' +
                    '</div>' +
                    '<button type="button" class="btn btn-sm btn-outline-primary">Add</button>';

                div.querySelector('button').addEventListener('click', function(e) {
                    e.stopPropagation();
                    addPromptToPackFromModal(p);
                    div.remove();
                    // Update empty message
                    if (resultsContainer.children.length === 0 && emptyMsg) {
                        emptyMsg.style.display = 'block';
                    }
                });

                resultsContainer.appendChild(div);
            });
        } catch (error) {
            resultsContainer.innerHTML = '<div class="text-danger p-2">Error loading prompts</div>';
        }
    }

    async function addPromptToPackFromModal(prompt) {
        try {
            const response = await fetch('/api/packs/' + packId + '/items', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ prompt_id: prompt.id }),
            });
            const result = await response.json();

            if (response.ok) {
                // Remove "no items" row if present
                const noItems = document.getElementById('noItemsRow');
                if (noItems) noItems.remove();

                // Add row to table
                const tbody = document.getElementById('packItemsBody');
                if (tbody) {
                    const rowCount = tbody.querySelectorAll('tr[data-prompt-id]').length;
                    const tr = document.createElement('tr');
                    tr.setAttribute('data-prompt-id', prompt.id);

                    let avatarHtml;
                    if (prompt.image) {
                        avatarHtml = '<img class="prompt-avatar" src="/static/' + prompt.image + '" alt="">';
                    } else {
                        avatarHtml = '<div class="prompt-avatar-placeholder"><i class="fas fa-robot"></i></div>';
                    }

                    const notice = prompt.pack_notice_period_days > 0
                        ? (prompt.pack_notice_period_days >= 365
                            ? Math.floor(prompt.pack_notice_period_days / 365) + 'y'
                            : prompt.pack_notice_period_days + 'd')
                        : '<small class="text-muted">--</small>';

                    tr.innerHTML =
                        '<td><i class="fas fa-grip-vertical drag-handle"></i></td>' +
                        '<td class="item-order">' + (rowCount + 1) + '</td>' +
                        '<td>' + avatarHtml + '</td>' +
                        '<td>' + escapeHtml(prompt.name) + '</td>' +
                        '<td><small class="text-muted">' + escapeHtml(prompt.owner_username || '--') + '</small></td>' +
                        '<td>' + notice + '</td>' +
                        '<td class="text-end">' +
                            '<button type="button" class="btn btn-sm btn-outline-danger" ' +
                            'onclick="removePackItem(' + prompt.id + ', \'' + escapeHtml(prompt.name).replace(/'/g, "\\'") + '\')">' +
                            '<i class="fas fa-times"></i></button>' +
                        '</td>';
                    tbody.appendChild(tr);
                }
                if (typeof packItemsGroup !== 'undefined') packItemsGroup.markClean();
                NotificationModal.toast('"' + escapeHtml(prompt.name) + '" added to pack', 'success');
            } else {
                NotificationModal.toast(result.detail || 'Failed to add prompt', 'danger');
            }
        } catch (error) {
            NotificationModal.toast('Error: ' + error.message, 'danger');
        }
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.appendChild(document.createTextNode(text || ''));
        return div.innerHTML;
    }

    // -----------------------------------------------------------------------
    // Cover image upload
    // -----------------------------------------------------------------------

    function initCoverImage() {
        const container = document.getElementById('coverImageContainer');
        const previewContainer = document.getElementById('coverPreviewContainer');
        const fileInput = document.getElementById('coverImageInput');

        if (!container || !fileInput) return;

        // Click on edit icon or placeholder -> trigger file input
        container.querySelectorAll('.cover-icon.edit, .cover-placeholder').forEach(function(el) {
            el.addEventListener('click', function() { fileInput.click(); });
        });

        if (previewContainer) {
            previewContainer.querySelectorAll('.cover-icon.edit').forEach(function(el) {
                el.addEventListener('click', function() { fileInput.click(); });
            });
            previewContainer.querySelectorAll('.cover-icon.cancel').forEach(function(el) {
                el.addEventListener('click', function() { cancelCoverPreview(); });
            });
        }

        // Delete icon
        container.querySelectorAll('.cover-icon.delete').forEach(function(el) {
            el.addEventListener('click', function() { deleteCoverImage(); });
        });

        // File selected -> show preview
        fileInput.addEventListener('change', function(e) {
            var file = e.target.files[0];
            if (!file) return;
            var reader = new FileReader();
            reader.onload = function(ev) {
                document.getElementById('coverPreviewImage').src = ev.target.result;
                container.classList.add('hidden');
                previewContainer.classList.remove('hidden');
            };
            reader.readAsDataURL(file);
        });
    }

    async function uploadCoverImage(targetPackId) {
        var fileInput = document.getElementById('coverImageInput');
        if (!fileInput || !fileInput.files[0]) return null;

        var formData = new FormData();
        formData.append('file', fileInput.files[0]);

        var response = await fetch('/api/packs/' + targetPackId + '/cover-image', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            var data = await response.json();
            throw new Error(data.detail || 'Failed to upload cover image');
        }

        return await response.json();
    }

    function deleteCoverImage() {
        var currentPackId = document.getElementById('packId')?.value;
        if (!currentPackId) return;

        NotificationModal.confirm('Remove Cover Image', 'Remove cover image?', async () => {
            try {
                var response = await fetch('/api/packs/' + currentPackId + '/cover-image', {
                    method: 'DELETE'
                });

                if (response.ok) {
                    location.reload();
                } else {
                    var data = await response.json();
                    NotificationModal.toast(data.detail || 'Failed to remove cover image', 'danger');
                }
            } catch (error) {
                NotificationModal.toast('Error: ' + error.message, 'danger');
            }
        }, null, { type: 'error', confirmText: 'Remove' });
    }

    function cancelCoverPreview() {
        var container = document.getElementById('coverImageContainer');
        var previewContainer = document.getElementById('coverPreviewContainer');
        var fileInput = document.getElementById('coverImageInput');

        container.classList.remove('hidden');
        previewContainer.classList.add('hidden');
        fileInput.value = '';
    }

    function hasPendingCoverImage() {
        var fileInput = document.getElementById('coverImageInput');
        return fileInput && fileInput.files && fileInput.files.length > 0;
    }

    // Initialize cover image on load
    initCoverImage();

    // -----------------------------------------------------------------------
    // Sales / Purchase History
    // -----------------------------------------------------------------------

    function loadPurchases(id) {
        var loadingEl  = document.getElementById('salesLoading');
        var emptyEl    = document.getElementById('salesEmpty');
        var summaryEl  = document.getElementById('salesSummary');
        var wrapperEl  = document.getElementById('salesTableWrapper');
        var tbodyEl    = document.getElementById('salesTableBody');
        var countEl    = document.getElementById('salesCount');
        var revenueEl  = document.getElementById('salesRevenue');

        if (!loadingEl) return; // Section not rendered (new pack)

        fetch('/api/packs/' + id + '/purchases')
            .then(function(res) {
                if (!res.ok) throw new Error('Failed to load purchases');
                return res.json();
            })
            .then(function(data) {
                loadingEl.style.display = 'none';
                var purchases = data.purchases || [];

                countEl.textContent   = data.total_count || 0;
                revenueEl.textContent = (data.total_revenue || 0).toFixed(2);
                summaryEl.style.display = '';

                if (purchases.length === 0) {
                    emptyEl.style.display = '';
                    return;
                }

                tbodyEl.innerHTML = '';
                for (var i = 0; i < purchases.length; i++) {
                    var p  = purchases[i];
                    var tr = document.createElement('tr');

                    // Format date
                    var dateStr = '';
                    if (p.created_at) {
                        var d = new Date(p.created_at);
                        dateStr = d.toLocaleDateString() + ' ' + d.toLocaleTimeString();
                    }

                    // Status badge color
                    var statusClass = 'bg-secondary';
                    if (p.status === 'completed') statusClass = 'bg-success';
                    else if (p.status === 'refunded') statusClass = 'bg-danger';
                    else if (p.status === 'pending') statusClass = 'bg-warning text-dark';

                    tr.innerHTML =
                        '<td>' + escapeHtml(dateStr) + '</td>' +
                        '<td>' + escapeHtml(p.username) + '</td>' +
                        '<td>' + escapeHtml(p.email) + '</td>' +
                        '<td class="text-end">$' + Number(p.amount || 0).toFixed(2) + '</td>' +
                        '<td>' + escapeHtml(p.payment_method || '-') + '</td>' +
                        '<td><span class="badge ' + statusClass + '">' + escapeHtml(p.status || 'unknown') + '</span></td>';

                    tbodyEl.appendChild(tr);
                }
                wrapperEl.style.display = '';
            })
            .catch(function(err) {
                loadingEl.style.display = 'none';
                emptyEl.textContent = 'Error loading sales data.';
                emptyEl.style.display = '';
            });
    }

    // Load purchases for existing packs
    if (packId) {
        loadPurchases(packId);
    }

})();
