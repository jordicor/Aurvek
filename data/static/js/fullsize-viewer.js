/**
 * Fullsize Image Viewer - Reusable component
 *
 * Usage:
 *   FullsizeViewer.init({
 *       showNav: true,           // Show prev/next arrows
 *       showDownload: true,      // Show download button
 *       showDelete: false,       // Show delete button
 *       transformUrl: true,      // Try to load _fullsize version (false = show original)
 *       onDelete: (url, index, imageData) => { ... },  // Delete callback
 *       images: []               // Array of image URLs/objects for navigation
 *   });
 *
 *   // Then on image click:
 *   onclick="FullsizeViewer.show('url', index)"
 */

const FullsizeViewer = (function() {
    // Private state
    let config = {
        showNav: false,
        showDownload: true,
        showDelete: false,
        transformUrl: true,  // Try to load _fullsize version
        onDelete: null,
        images: []
    };
    let currentIndex = 0;
    let container = null;
    let initialized = false;
    let renderGeneration = 0;
    let pendingImage = null;
    let displayedResource = null;

    function cancelPendingImage() {
        if (!pendingImage) return;
        pendingImage.onload = null;
        pendingImage.onerror = null;
        try { pendingImage.src = ''; } catch (error) { /* already released */ }
        pendingImage = null;
    }

    function setDeleteEnabled(enabled) {
        if (!container) return;
        const deleteBtn = container.querySelector('.fullsize-viewer-delete');
        if (deleteBtn) deleteBtn.disabled = !enabled;
    }

    function snapshotImageData(index) {
        const item = config.images[index];
        if (!item || typeof item === 'string') return item || null;
        return Object.freeze({ ...item });
    }

    // Inject HTML into DOM
    function injectHTML() {
        if (document.getElementById('fullsizeViewer')) return;

        const html = `
            <div id="fullsizeViewer" class="fullsize-viewer">
                <div class="fullsize-viewer-backdrop"></div>
                <div class="fullsize-viewer-spinner">
                    <div class="spinner-border text-light" role="status">
                        <span class="visually-hidden"></span>
                    </div>
                </div>
                <img id="fullsizeViewerImage" class="fullsize-viewer-image" src="" alt="">
                <button type="button" class="fullsize-viewer-close" aria-label="">&times;</button>
                <div class="fullsize-viewer-nav fullsize-viewer-prev" title="">
                    <i class="fas fa-chevron-left"></i>
                </div>
                <div class="fullsize-viewer-nav fullsize-viewer-next" title="">
                    <i class="fas fa-chevron-right"></i>
                </div>
                <div class="fullsize-viewer-controls">
                    <button class="btn btn-primary fullsize-viewer-download" title="">
                        <i class="fas fa-download me-1"></i><span></span>
                    </button>
                    <button class="btn btn-danger fullsize-viewer-delete" title="">
                        <i class="fas fa-trash me-1"></i><span></span>
                    </button>
                </div>
            </div>
        `;
        document.body.insertAdjacentHTML('beforeend', html);
        container = document.getElementById('fullsizeViewer');
        const t = (key) => window.AurvekI18n.t(`chat_ui.${key}`);
        container.querySelector('.visually-hidden').textContent = t('viewer.loading');
        container.querySelector('.fullsize-viewer-image').alt = t('viewer.fullsize_image');
        container.querySelector('.fullsize-viewer-close').ariaLabel = t('action.close');
        container.querySelector('.fullsize-viewer-prev').title = t('viewer.previous');
        container.querySelector('.fullsize-viewer-next').title = t('viewer.next');
        const downloadButton = container.querySelector('.fullsize-viewer-download');
        downloadButton.title = t('viewer.download');
        downloadButton.querySelector('span').textContent = t('action.download');
        const deleteButton = container.querySelector('.fullsize-viewer-delete');
        deleteButton.title = t('viewer.delete');
        deleteButton.querySelector('span').textContent = t('action.delete');
    }

    // Bind event listeners
    function bindEvents() {
        // Close on backdrop click
        container.querySelector('.fullsize-viewer-backdrop').addEventListener('click', close);

        // Close button
        container.querySelector('.fullsize-viewer-close').addEventListener('click', close);

        // Navigation
        container.querySelector('.fullsize-viewer-prev').addEventListener('click', prev);
        container.querySelector('.fullsize-viewer-next').addEventListener('click', next);

        // Download
        container.querySelector('.fullsize-viewer-download').addEventListener('click', download);

        // Delete
        container.querySelector('.fullsize-viewer-delete').addEventListener('click', deleteImage);

        // Keyboard navigation
        document.addEventListener('keydown', handleKeydown);
    }

    function handleKeydown(e) {
        if (!container.classList.contains('active')) return;

        switch(e.key) {
            case 'Escape':
                close();
                break;
            case 'ArrowLeft':
                if (config.showNav) prev();
                break;
            case 'ArrowRight':
                if (config.showNav) next();
                break;
        }
    }

    // Update UI based on config
    function updateUI() {
        const navPrev = container.querySelector('.fullsize-viewer-prev');
        const navNext = container.querySelector('.fullsize-viewer-next');
        const downloadBtn = container.querySelector('.fullsize-viewer-download');
        const deleteBtn = container.querySelector('.fullsize-viewer-delete');
        const controls = container.querySelector('.fullsize-viewer-controls');

        // Show/hide navigation
        const showNavigation = config.showNav && config.images.length > 1;
        navPrev.style.display = showNavigation ? 'flex' : 'none';
        navNext.style.display = showNavigation ? 'flex' : 'none';

        // Show/hide buttons
        downloadBtn.style.display = config.showDownload ? 'inline-flex' : 'none';
        deleteBtn.style.display = config.showDelete ? 'inline-flex' : 'none';
        deleteBtn.disabled = !displayedResource;

        // Hide controls container if both buttons are hidden
        const hasVisibleButtons = config.showDownload || config.showDelete;
        controls.style.display = hasVisibleButtons ? 'flex' : 'none';
    }

    // Public methods
    function init(options = {}) {
        config = { ...config, ...options };

        if (!initialized) {
            injectHTML();
            bindEvents();
            initialized = true;
        }

        updateUI();
    }

    function show(url, index = 0) {
        if (!initialized) init();
        if (typeof url !== 'string' || !url) return;

        const image = container.querySelector('.fullsize-viewer-image');
        const spinner = container.querySelector('.fullsize-viewer-spinner');

        currentIndex = index;
        const generation = ++renderGeneration;
        const imageData = snapshotImageData(index);
        const requestedResource = Object.freeze({
            generation,
            index,
            requestedUrl: url,
            imageData,
        });
        cancelPendingImage();
        displayedResource = null;
        setDeleteEnabled(false);

        // Show container and spinner
        container.classList.add('active');
        spinner.style.display = 'flex';
        image.style.opacity = '0';

        // Try fullsize version first (if transformUrl enabled), fallback to original
        let finalUrl = url;
        if (config.transformUrl) {
            const fullsizeUrl = url.replace('_256.webp', '_fullsize.webp')
                                   .replace('_128.webp', '_fullsize.webp')
                                   .replace('_64.webp', '_fullsize.webp')
                                   .replace('_32.webp', '_fullsize.webp');
            finalUrl = fullsizeUrl !== url ? fullsizeUrl : url;
        }

        const loadCandidate = (candidateUrl, allowFallback) => {
            if (generation !== renderGeneration) return;
            const loader = new Image();
            pendingImage = loader;
            loader.onload = function() {
                if (
                    generation !== renderGeneration
                    || pendingImage !== loader
                    || !container.classList.contains('active')
                ) {
                    return;
                }

                pendingImage = null;
                const loadedUrl = this.currentSrc || this.src || candidateUrl;
                spinner.style.display = 'none';
                image.src = loadedUrl;
                image.style.opacity = '1';
                displayedResource = Object.freeze({
                    ...requestedResource,
                    loadedUrl,
                });
                setDeleteEnabled(true);
            };
            loader.onerror = function() {
                if (generation !== renderGeneration || pendingImage !== loader) return;
                pendingImage = null;
                if (allowFallback && candidateUrl !== url) {
                    loadCandidate(url, false);
                    return;
                }

                spinner.style.display = 'none';
                image.src = '';
                image.style.opacity = '0';
                displayedResource = null;
                setDeleteEnabled(false);
            };
            loader.src = candidateUrl;
        };

        loadCandidate(finalUrl, finalUrl !== url);

        updateUI();
    }

    function close() {
        if (!container) return;
        renderGeneration += 1;
        cancelPendingImage();
        displayedResource = null;
        setDeleteEnabled(false);
        container.classList.remove('active');
        const image = container.querySelector('.fullsize-viewer-image');
        image.src = '';
    }

    function prev() {
        if (!config.images.length) return;
        currentIndex = (currentIndex - 1 + config.images.length) % config.images.length;
        const url = typeof config.images[currentIndex] === 'string'
            ? config.images[currentIndex]
            : config.images[currentIndex].url;
        show(url, currentIndex);
    }

    function next() {
        if (!config.images.length) return;
        currentIndex = (currentIndex + 1) % config.images.length;
        const url = typeof config.images[currentIndex] === 'string'
            ? config.images[currentIndex]
            : config.images[currentIndex].url;
        show(url, currentIndex);
    }

    function download() {
        if (!displayedResource || displayedResource.generation !== renderGeneration) return;

        const link = document.createElement('a');
        link.href = displayedResource.loadedUrl;

        // Extract filename from URL or generate random
        const urlParts = displayedResource.loadedUrl.split('/');
        const filename = urlParts[urlParts.length - 1] || `image_${Date.now()}.webp`;
        link.download = filename;

        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }

    function deleteImage() {
        if (
            !config.onDelete
            || !displayedResource
            || displayedResource.generation !== renderGeneration
        ) return;

        const resource = displayedResource;
        config.onDelete(resource.requestedUrl, resource.index, resource.imageData);
    }

    // Method to update images array (for dynamic content)
    function setImages(images) {
        config.images = Array.isArray(images) ? images : [];
        updateUI();
    }

    // Public API
    return {
        init,
        show,
        close,
        prev,
        next,
        download,
        setImages,
        // Expose for external access if needed
        getCurrentIndex: () => currentIndex,
        getDisplayedResource: () => displayedResource ? { ...displayedResource } : null,
        getConfig: () => ({ ...config })
    };
})();

// Make it globally available
window.FullsizeViewer = FullsizeViewer;
