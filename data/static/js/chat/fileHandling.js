const MAX_PDF_SIZE_MB = 25;
const MAX_IMAGE_SIZE_MB = 20;
const MAX_TEXT_SIZE_MB = 2;
const DEFAULT_ATTACHMENT_UPLOAD_CHUNK_SIZE = 2 * 1024 * 1024;

// Upload resilience: adaptive chunk sizing, per-chunk retry, cheap resume.
const UPLOAD_MIN_CHUNK = 256 * 1024;          // == server ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES
const UPLOAD_MAX_CHUNK = 8 * 1024 * 1024;     // == server ATTACHMENT_UPLOAD_MAX_CHUNK_SIZE_BYTES
const UPLOAD_TARGET_CHUNK_SECONDS = 4;
const UPLOAD_CHUNK_RETRY_DELAYS_MS = [600, 1800, 4500, 9000]; // backoff before retries 1..4 (jitter added per attempt)
const UPLOAD_CHUNK_MAX_ATTEMPTS = UPLOAD_CHUNK_RETRY_DELAYS_MS.length + 1; // 1 initial attempt + 4 retries
const UPLOAD_TIMEOUT_FLOOR_MS = 60000;
const UPLOAD_TIMEOUT_CAP_MS = 170000;         // < nginx client_body_timeout 180s
const UPLOAD_TIMEOUT_MIN_BPS = 32 * 1024;     // 32 KB/s floor for the whole-request deadline
const UPLOAD_MAX_FULL_RETRIES_BEFORE_SHRINK = 2;
// Connection-scoped quality signal (intentionally global, shared across files):
// a persistently failing upload biases later fresh uploads smaller until one
// upload fully succeeds (which resets recentFailures). entry.failures is the
// separate per-File counter that only governs when to drop the resume entry.
const uploadProfile = { ewmaBytesPerSec: 0, recentFailures: 0 };
const resumeRegistry = new WeakMap();         // File -> { uploadId, chunkSize, conversationId, failures }
const uploadSleep = (ms) => new Promise((r) => setTimeout(r, ms));

const TEXT_FILE_EXTENSIONS = new Set([
    '.txt', '.md', '.csv', '.json', '.xml', '.html', '.htm',
    '.py', '.js', '.ts', '.css', '.sql', '.yaml', '.yml', '.toml',
    '.ini', '.cfg', '.conf', '.log', '.sh', '.bash',
    '.java', '.c', '.cpp', '.h', '.hpp', '.go', '.rs', '.rb',
    '.php', '.r', '.swift', '.kt', '.lua'
]);

const COMPRESSION_THRESHOLD_BYTES = 3 * 1024 * 1024; // 3MB - matches server SIZE_THRESHOLD
const COMPRESSION_QUALITY = 0.85;
const MAX_SHRINK_STEPS = 10;
const COMPRESSIBLE_TYPES = new Set([
    'image/png', 'image/bmp', 'image/tiff', 'image/gif',
    'image/jpeg', 'image/webp'
]);

// Tracks in-flight compression operations. Exported for chat.js send guard.
let compressionInProgress = 0;

function trackAttachmentPreview(file, element) {
    if (file && element) {
        element._aurvekAttachedFile = file;
    }
    return element;
}

function removeAttachedFileBatch(files) {
    const batch = new Set(Array.from(files || []));
    if (batch.size === 0) return;

    attachedFiles = attachedFiles.filter(file => !batch.has(file));

    const imagePreviews = document.getElementById('image-previews');
    if (!imagePreviews) return;
    Array.from(imagePreviews.children).forEach(preview => {
        if (batch.has(preview._aurvekAttachedFile)) {
            preview.remove();
        }
    });
    imagePreviews.classList.toggle('hidden', imagePreviews.children.length === 0);
    const fileInput = document.getElementById('chat-files');
    if (fileInput) fileInput.value = '';
}

function attachmentIsStillQueued(file) {
    return attachedFiles.includes(file);
}

window.trackAttachmentPreview = trackAttachmentPreview;
window.removeAttachedFileBatch = removeAttachedFileBatch;
window.attachmentIsStillQueued = attachmentIsStillQueued;

function getCompressionTargetBytes() {
    const sizeMb = Number(Config.max_api_image_size_mb);
    if (Number.isFinite(sizeMb) && sizeMb > 0) {
        return sizeMb * 1024 * 1024;
    }
    // Safe fallback: matches the current backend default in common.py.
    return 5 * 1024 * 1024;
}

// ORDERING HAZARD (do NOT revert this to a module-level const):
// fileHandling.js is loaded by chat.html at line 714. The inline <script>
// block that assigns Config.max_chat_image_dimension runs at chat.html:737,
// AFTER this file has already been parsed and its top-level const declarations
// have been evaluated. A module-level
//     const COMPRESSION_MAX_DIMENSION = Config.max_chat_image_dimension || 1568;
// would always resolve to the fallback 1568 on initial page load, silently
// defeating the backend -> frontend Config channel. If the cap is ever tuned
// in common.py, a module-level const would NOT pick it up on reload.
// Read inside the function body instead, mirroring getCompressionTargetBytes()
// above. This is the F1 fix from round 8 external review.
function getCompressionMaxDimension() {
    const dim = Number(Config.max_chat_image_dimension);
    if (Number.isFinite(dim) && dim > 0) {
        return dim;
    }
    // Safe fallback: matches common.py's MAX_CHAT_IMAGE_DIMENSION.
    return 1568;
}

/**
 * Compress an image file to WebP if it exceeds the size threshold.
 * Uses Canvas API -- no external libraries.
 *
 * @param {File|Blob} file - The image file to potentially compress
 * @returns {Promise<File|Blob>} - Compressed file, or original if compression
 *   is not beneficial, not applicable, or fails
 */
async function maybeCompressImage(file) {
    // Only compress image types we know how to handle
    if (!file.type || !COMPRESSIBLE_TYPES.has(file.type)) return file;

    try {
        const compressionTargetBytes = getCompressionTargetBytes();
        const maxDim = getCompressionMaxDimension();
        const bitmap = await createImageBitmap(file);
        const exceedsDimensionCap = bitmap.width > maxDim || bitmap.height > maxDim;

        if (file.size <= COMPRESSION_THRESHOLD_BYTES && !exceedsDimensionCap) {
            bitmap.close();
            return file;
        }

        let width = bitmap.width;
        let height = bitmap.height;
        let compressedBlob = null;

        // Scale down first if any dimension exceeds the hard cap.
        if (exceedsDimensionCap) {
            const scale = maxDim / Math.max(width, height);
            width = Math.round(width * scale);
            height = Math.round(height * scale);
        }

        try {
            for (let step = 0; step < MAX_SHRINK_STEPS; step++) {
                const canvas = new OffscreenCanvas(width, height);
                const ctx = canvas.getContext('2d');
                if (!ctx) return file;

                ctx.drawImage(bitmap, 0, 0, width, height);
                compressedBlob = await canvas.convertToBlob({
                    type: 'image/webp',
                    quality: COMPRESSION_QUALITY
                });

                if (!compressedBlob || compressedBlob.size === 0) return file;
                if (compressedBlob.size <= compressionTargetBytes) break;
                if (width === 1 && height === 1) break;

                // Try again with a smaller frame if we are still above the backend gate.
                const scale = 0.85;
                width = Math.max(1, Math.round(width * scale));
                height = Math.max(1, Math.round(height * scale));
            }
        } finally {
            bitmap.close();
        }

        if (!compressedBlob || compressedBlob.size === 0) return file;

        // If we still could not fit under the backend gate, keep the original
        // so the existing server-side path can make the final decision.
        if (compressedBlob.size > compressionTargetBytes) return file;

        // Only use compressed version if it is actually smaller.
        if (compressedBlob.size >= file.size && !exceedsDimensionCap) return file;

        // Build a proper File with a corrected name (.webp extension)
        const originalName = file.name || 'image';
        const baseName = originalName.replace(/\.[^.]+$/, '');
        const compressedFile = new File(
            [compressedBlob],
            baseName + '.webp',
            { type: 'image/webp', lastModified: Date.now() }
        );

        const originalMB = (file.size / (1024 * 1024)).toFixed(1);
        const compressedMB = (compressedFile.size / (1024 * 1024)).toFixed(1);
        console.log(`[compression] ${originalName}: ${originalMB}MB -> ${compressedMB}MB`);

        return compressedFile;
    } catch (err) {
        // Fallback: send original file if Canvas compression fails
        console.warn('[compression] Failed, sending original:', err);
        return file;
    }
}

function isTextFile(file) {
    if (!file.name) return false;
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    if (!TEXT_FILE_EXTENSIONS.has(ext)) return false;
    const MIME_EXCEPTIONS = new Set(['video/mp2t']);
    if (file.type && !MIME_EXCEPTIONS.has(file.type)
        && (file.type.startsWith('image/') || file.type === 'application/pdf'
        || file.type.startsWith('audio/') || file.type.startsWith('video/'))) return false;
    return true;
}

function isAcceptedFileType(file) {
    return file.type.startsWith('image/') || file.type === 'application/pdf' || isTextFile(file);
}

function validateFileSize(file) {
    const sizeMB = file.size / (1024 * 1024);
    if (file.type === 'application/pdf' && sizeMB > MAX_PDF_SIZE_MB) {
        NotificationModal.warning(() => AurvekI18n.t('chat_widgets.files.too_large_title'), () => AurvekI18n.t('chat_widgets.files.pdf_too_large', {size: MAX_PDF_SIZE_MB}));
        return false;
    } else if (isTextFile(file)) {
        if (sizeMB > MAX_TEXT_SIZE_MB) {
            NotificationModal.warning(() => AurvekI18n.t('chat_widgets.files.too_large_title'), () => AurvekI18n.t('chat_widgets.files.text_too_large', {size: MAX_TEXT_SIZE_MB}));
            return false;
        }
    } else if (file.type.startsWith('image/') && sizeMB > MAX_IMAGE_SIZE_MB) {
        NotificationModal.warning(() => AurvekI18n.t('chat_widgets.files.too_large_title'), () => AurvekI18n.t('chat_widgets.files.image_too_large', {size: MAX_IMAGE_SIZE_MB}));
        return false;
    }
    return true;
}

async function handlePasteEvent(event) {

    if (!Config.can_send_files) return;
    const items = (event.clipboardData || event.originalEvent.clipboardData).items;

    // Collect image blobs first (clipboard items are ephemeral -- extract immediately)
    const imageBlobs = [];
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === 'file' && item.type.startsWith('image/')) {
            const blob = item.getAsFile();
            if (!blob || !validateFileSize(blob)) continue;
            imageBlobs.push(blob);
        }
    }

    if (imageBlobs.length === 0) return;

    compressionInProgress++;
    try {
        let compressedCount = 0;
        let totalSavedBytes = 0;

        for (const blob of imageBlobs) {
            const processed = await maybeCompressImage(blob);

            attachedFiles.push(processed);
            const reader = new FileReader();
            reader.onload = function(event){
                if (!attachmentIsStillQueued(processed)) return;
                const img = document.createElement('img');
                img.src = event.target.result;
                img.className = 'preview-image';
                trackAttachmentPreview(processed, img);
                document.getElementById('image-previews').appendChild(img);
                document.getElementById('image-previews').classList.remove('hidden');
            };
            reader.readAsDataURL(processed);

            if (processed !== blob && processed.size < blob.size) {
                compressedCount++;
                totalSavedBytes += blob.size - processed.size;
            }
        }

        if (compressedCount > 0) {
            const saved = new Intl.NumberFormat(AurvekI18n.locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(totalSavedBytes / (1024 * 1024));
            const msg = AurvekI18n.t('chat_widgets.files.images_compressed', {count: compressedCount, saved});
            NotificationModal.toast(msg, 'info', 3000);
        }
    } finally {
        compressionInProgress--;
    }

    setTimeout(() => {
        document.getElementById('message-text').focus();
    }, 0);
}

function getRangedPdfDisplayName(filename, rangeOptions = {}) {
    const start = parseInt(rangeOptions.pdfPageStart || '', 10);
    const end = parseInt(rangeOptions.pdfPageEnd || '', 10);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start) {
        return filename;
    }
    const name = filename || 'document.pdf';
    const dotIndex = name.lastIndexOf('.');
    const root = dotIndex > 0 ? name.slice(0, dotIndex) : name;
    const ext = dotIndex > 0 ? name.slice(dotIndex) : '.pdf';
    return `${root || 'document'}_pages_${start}-${end}${ext || '.pdf'}`;
}


function processFiles(files, formData, imagePreviews, targetConversationId = null, displayOptions = {}) {
    const renderedAttachmentElements = [];
    renderedAttachmentElements.cancelled = false;
    for (var i = 0; i < files.length; i++) {
        if (!isAcceptedFileType(files[i]) || !validateFileSize(files[i])) {
            if (!isAcceptedFileType(files[i])) {
                NotificationModal.warning(() => AurvekI18n.t('chat_widgets.files.invalid_title'), () => AurvekI18n.t('chat_widgets.files.invalid_type'));
            }
            continue;
        }

        formData.append('file', files[i]);

        if (files[i].type === 'application/pdf') {
            // Show PDF attachment in chat (mirrors the image pattern)
            var userMessageElement = document.createElement('div');
            userMessageElement.classList.add('message', 'user');
            var pdfAttachment = document.createElement('div');
            pdfAttachment.className = 'chat-pdf-attachment';
            var badge = document.createElement('span');
            badge.className = 'pdf-badge';
            badge.textContent = 'PDF';
            var label = document.createElement('span');
            label.textContent = ' ' + getRangedPdfDisplayName(files[i].name, displayOptions);
            pdfAttachment.appendChild(badge);
            pdfAttachment.appendChild(label);
            userMessageElement.appendChild(pdfAttachment);

            var chatMessagesContainer = document.getElementById('chat-messages-container');
            chatMessagesContainer.appendChild(userMessageElement);
            renderedAttachmentElements.push(userMessageElement);
            var chatWindow = document.getElementById('chat-window');
            chatWindow.scrollTop = chatWindow.scrollHeight;

            imagePreviews.innerHTML = '';
            imagePreviews.classList.add('hidden');
            document.getElementById('chat-files').value = '';
        } else if (isTextFile(files[i])) {
            imagePreviews.innerHTML = '';
            imagePreviews.classList.add('hidden');
            document.getElementById('chat-files').value = '';

            var msgDiv = document.createElement('div');
            msgDiv.className = 'message user';
            var textAttachment = document.createElement('div');
            textAttachment.className = 'chat-text-attachment';
            var badge = document.createElement('span');
            badge.className = 'text-badge';
            badge.textContent = 'TXT';
            var label = document.createElement('span');
            label.textContent = files[i].name;
            textAttachment.appendChild(badge);
            textAttachment.appendChild(label);
            msgDiv.appendChild(textAttachment);
            var chatMessagesContainer = document.getElementById('chat-messages-container');
            chatMessagesContainer.appendChild(msgDiv);
            renderedAttachmentElements.push(msgDiv);
            var chatWindow = document.getElementById('chat-window');
            chatWindow.scrollTop = chatWindow.scrollHeight;
        } else {
            // Existing image preview logic
            var reader = new FileReader();
            reader.onload = function (e) {
                if (
                    renderedAttachmentElements.cancelled ||
                    (targetConversationId !== null &&
                        typeof currentConversationId !== 'undefined' &&
                        currentConversationId !== targetConversationId)
                ) {
                    return;
                }
                var img = document.createElement('img');
                img.src = e.target.result;
                img.className = 'preview-image';
                imagePreviews.appendChild(img);

                var userMessageElement = document.createElement('div');
                userMessageElement.classList.add('message', 'user');

                var userMessageImage = document.createElement('img');
                userMessageImage.src = e.target.result;
                userMessageImage.style.maxWidth = '100%';
                userMessageImage.style.height = 'auto';
                userMessageImage.style.display = 'block';
                userMessageImage.style.margin = '0 auto';

                userMessageElement.appendChild(userMessageImage);

                var chatMessagesContainer = document.getElementById('chat-messages-container');
                chatMessagesContainer.appendChild(userMessageElement);
                renderedAttachmentElements.push(userMessageElement);

                var chatWindow = document.getElementById('chat-window');
                chatWindow.scrollTop = chatWindow.scrollHeight;

                imagePreviews.innerHTML = '';
                imagePreviews.classList.add('hidden');
                document.getElementById('chat-files').value = '';
            };
            reader.onerror = function (e) {
                console.error('Error reading file:', e);
            };
            reader.readAsDataURL(files[i]);
        }
    }
    if (imagePreviews.children.length > 0) {
        imagePreviews.classList.remove('hidden');
    }
    return renderedAttachmentElements;
}

function getUploadContentType(file) {
    if (file.type) return file.type;
    const lowerName = (file.name || '').toLowerCase();
    if (lowerName.endsWith('.pdf')) return 'application/pdf';
    if (isTextFile(file)) return 'text/plain';
    return 'application/octet-stream';
}

function getAttachmentUploadChunkSize() {
    const configured = Number(Config.attachment_upload_chunk_size_bytes);
    if (Number.isFinite(configured) && configured > 0) {
        return Math.floor(configured);
    }
    return DEFAULT_ATTACHMENT_UPLOAD_CHUNK_SIZE;
}

function createAttachmentUploadId() {
    const bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
        window.crypto.getRandomValues(bytes);
        return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    }
    return `${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 14)}`;
}

function recordChunkThroughput(bytes, seconds) {
    if (!(bytes > 0) || !(seconds > 0)) return;
    const sample = bytes / seconds;
    const alpha = 0.3;
    uploadProfile.ewmaBytesPerSec = uploadProfile.ewmaBytesPerSec > 0
        ? (alpha * sample) + ((1 - alpha) * uploadProfile.ewmaBytesPerSec)
        : sample;
}

function recordChunkFailure() {
    uploadProfile.recentFailures = Math.min(uploadProfile.recentFailures + 1, 4);
}

function pickAdaptiveChunkSize() {
    const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (conn && (conn.saveData || conn.effectiveType === '2g' || conn.effectiveType === 'slow-2g')) {
        return UPLOAD_MIN_CHUNK;
    }
    let base;
    if (uploadProfile.ewmaBytesPerSec > 0) {
        base = uploadProfile.ewmaBytesPerSec * UPLOAD_TARGET_CHUNK_SECONDS;
    } else if (conn && conn.downlink > 0) {
        base = (conn.downlink * 1_000_000 / 8) * UPLOAD_TARGET_CHUNK_SECONDS; // Mbps -> bytes/s
    } else {
        base = getAttachmentUploadChunkSize();
    }
    base /= 2 ** uploadProfile.recentFailures;
    return Math.max(UPLOAD_MIN_CHUNK, Math.min(UPLOAD_MAX_CHUNK, Math.floor(base)));
}

function chunkTimeoutMs(chunkBytes) {
    return Math.max(
        UPLOAD_TIMEOUT_FLOOR_MS,
        Math.min(UPLOAD_TIMEOUT_CAP_MS, (chunkBytes / UPLOAD_TIMEOUT_MIN_BPS) * 1000)
    );
}

function updateAttachmentUploadStatus(rendered, progress, text) {
    if (!rendered) return;
    const clamped = Math.max(0, Math.min(100, Math.round(progress)));
    if (rendered.progressFill) {
        rendered.progressFill.style.width = `${clamped}%`;
    }
    if (rendered.status) {
        if (typeof text === 'function') AurvekI18n.bindValue(rendered.status, text);
        else AurvekI18n.bindValue(rendered.status, () => text || `${new Intl.NumberFormat(AurvekI18n.locale).format(clamped)}%`);
    }
}

function markAttachmentUploadComplete(rendered) {
    if (!rendered) return;
    if (rendered.progressWrap) {
        rendered.progressWrap.remove();
        rendered.progressWrap = null;
    }
    if (rendered.status) {
        const inlineAttachment = rendered.status.closest('.chat-pdf-attachment, .chat-text-attachment');
        if (!inlineAttachment && rendered.status.parentElement) {
            rendered.status.parentElement.style.textAlign = 'center';
        }
        AurvekI18n.unbind(rendered.status);
        rendered.status.textContent = '';
        rendered.status.innerHTML = '<i class="fas fa-check-circle" aria-hidden="true"></i>';
        AurvekI18n.bindAttribute(rendered.status, 'title', 'chat_widgets.files.uploaded');
        AurvekI18n.bindAttribute(rendered.status, 'aria-label', 'chat_widgets.files.uploaded');
        rendered.status.setAttribute('role', 'img');
        Object.assign(rendered.status.style, {
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginTop: inlineAttachment ? '0' : '6px',
            marginLeft: '0',
            fontSize: '0.95rem',
            lineHeight: '1',
            opacity: '1',
            color: '#22c55e',
            verticalAlign: 'middle'
        });
    }
}

function markAttachmentUploadFailed(rendered, message) {
    if (!rendered) return;
    if (rendered.progressFill) {
        rendered.progressFill.style.background = '#dc3545';
    }
    if (rendered.status) {
        if (window.AurvekEmbed || !message) AurvekI18n.bindText(rendered.status, 'chat_widgets.files.failed');
        else {
            AurvekI18n.unbind(rendered.status);
            rendered.status.textContent = message;
        }
    }
}

function createAttachmentUploadEcho(file, targetConversationId = null, displayOptions = {}) {
    const chatMessagesContainer = document.getElementById('chat-messages-container');
    const chatWindow = document.getElementById('chat-window');
    const userMessageElement = document.createElement('div');
    userMessageElement.classList.add('message', 'user');
    if (targetConversationId !== null) {
        userMessageElement.dataset.conversationId = targetConversationId;
    }

    const progressWrap = document.createElement('div');
    progressWrap.className = 'attachment-upload-progress';
    Object.assign(progressWrap.style, {
        width: '100%',
        height: '4px',
        marginTop: '8px',
        borderRadius: '4px',
        overflow: 'hidden',
        background: 'rgba(255,255,255,0.18)'
    });
    const progressFill = document.createElement('div');
    Object.assign(progressFill.style, {
        width: '0%',
        height: '100%',
        borderRadius: '4px',
        background: '#20c997',
        transition: 'width 0.16s ease'
    });
    progressWrap.appendChild(progressFill);

    const status = document.createElement('span');
    status.className = 'attachment-upload-status';
    status.textContent = '0%';
    Object.assign(status.style, {
        display: 'block',
        marginTop: '5px',
        fontSize: '0.78rem',
        opacity: '0.8'
    });

    if (file.type === 'application/pdf') {
        const pdfAttachment = document.createElement('div');
        pdfAttachment.className = 'chat-pdf-attachment';
        const badge = document.createElement('span');
        badge.className = 'pdf-badge';
        badge.textContent = 'PDF';
        const label = document.createElement('span');
        label.textContent = ' ' + getRangedPdfDisplayName(file.name, displayOptions);
        pdfAttachment.appendChild(badge);
        pdfAttachment.appendChild(label);
        pdfAttachment.appendChild(progressWrap);
        pdfAttachment.appendChild(status);
        userMessageElement.appendChild(pdfAttachment);
    } else if (isTextFile(file)) {
        const textAttachment = document.createElement('div');
        textAttachment.className = 'chat-text-attachment';
        const badge = document.createElement('span');
        badge.className = 'text-badge';
        badge.textContent = 'TXT';
        const label = document.createElement('span');
        label.textContent = file.name;
        textAttachment.appendChild(badge);
        textAttachment.appendChild(label);
        textAttachment.appendChild(progressWrap);
        textAttachment.appendChild(status);
        userMessageElement.appendChild(textAttachment);
    } else {
        const imageWrap = document.createElement('div');
        const userMessageImage = document.createElement('img');
        const objectUrl = URL.createObjectURL(file);
        userMessageImage.src = objectUrl;
        userMessageImage.onload = () => URL.revokeObjectURL(objectUrl);
        userMessageImage.style.maxWidth = '100%';
        userMessageImage.style.height = 'auto';
        userMessageImage.style.display = 'block';
        userMessageImage.style.margin = '0 auto';
        imageWrap.appendChild(userMessageImage);
        imageWrap.appendChild(progressWrap);
        imageWrap.appendChild(status);
        userMessageElement.appendChild(imageWrap);
    }

    chatMessagesContainer.appendChild(userMessageElement);
    if (chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;

    return { element: userMessageElement, progressWrap, progressFill, status };
}

function postAttachmentForm(url, formData, { signal, onProgress, timeout } = {}) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        let abortedBySignal = false;

        xhr.open('POST', url, true);
        xhr.withCredentials = true;
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
        if (window.AurvekEmbed && new URL(url, window.location.href).origin === window.location.origin) {
            window.AurvekEmbed.requestHeaders().forEach((value, name) => xhr.setRequestHeader(name, value));
        }
        if (Number.isFinite(timeout) && timeout > 0) {
            xhr.timeout = timeout;
        }

        const abortHandler = () => {
            abortedBySignal = true;
            xhr.abort();
        };
        if (signal) {
            if (signal.aborted) {
                const error = new Error(AurvekI18n.t('chat_widgets.files.cancelled'));
                error.name = 'AbortError';
                reject(error);
                return;
            }
            signal.addEventListener('abort', abortHandler, { once: true });
        }

        xhr.upload.onprogress = (event) => {
            if (event.lengthComputable && typeof onProgress === 'function') {
                onProgress(event.loaded, event.total);
            }
        };
        xhr.onload = () => {
            if (signal) signal.removeEventListener('abort', abortHandler);
            let body = null;
            try {
                body = xhr.responseText ? JSON.parse(xhr.responseText) : null;
            } catch (err) {
                body = null;
            }
            if (xhr.status >= 200 && xhr.status < 300) {
                resolve(body || {});
                return;
            }
            const error = new Error(body?.message || body?.error || AurvekI18n.t('chat_widgets.files.upload_failed_status', {status: xhr.status}));
            error.uploadFailed = true;
            error.status = xhr.status;
            error.uploadRetryable = (xhr.status >= 500 || xhr.status === 408 || xhr.status === 429);
            reject(error);
        };
        xhr.onerror = () => {
            if (signal) signal.removeEventListener('abort', abortHandler);
            const error = new Error(AurvekI18n.t('chat_widgets.files.network_error'));
            error.uploadFailed = true;
            error.uploadRetryable = true;
            reject(error);
        };
        xhr.ontimeout = () => {
            if (signal) signal.removeEventListener('abort', abortHandler);
            const error = new Error(AurvekI18n.t('chat_widgets.files.timed_out'));
            error.uploadFailed = true;
            error.uploadRetryable = true;
            reject(error);
        };
        xhr.onabort = () => {
            if (signal) signal.removeEventListener('abort', abortHandler);
            const error = new Error(AurvekI18n.t(abortedBySignal ? 'chat_widgets.files.cancelled' : 'chat_widgets.files.aborted'));
            error.name = 'AbortError';
            reject(error);
        };
        xhr.send(formData);
    });
}

function getAttachmentRefsFromUploadElements(renderedAttachmentElements) {
    return (renderedAttachmentElements?.attachmentRefs || [])
        .map((attachment) => attachment?.attachment_ref)
        .filter(Boolean);
}

async function discardUploadedAttachmentRefs(conversationId, attachmentRefs) {
    const refs = Array.from(new Set((attachmentRefs || []).filter(Boolean)));
    if (!conversationId || refs.length === 0) return;
    const formData = new FormData();
    formData.append('attachment_refs', JSON.stringify(refs));
    try {
        await postAttachmentForm(`/api/conversations/${conversationId}/attachments/discard`, formData);
    } catch (error) {
        console.warn('Could not discard uploaded attachments:', error);
    }
}

function queryUploadStatus(conversationId, uploadId, expectedChunkSize, signal) {
    // Best-effort: any error, mismatch, or non-2xx -> empty Set (never throws,
    // never blocks a fresh attempt).
    return new Promise((resolve) => {
        let settled = false;
        const done = (value) => {
            if (settled) return;
            settled = true;
            resolve(value);
        };
        try {
            const xhr = new XMLHttpRequest();
            const url = `/api/conversations/${conversationId}/attachments/status?upload_id=${encodeURIComponent(uploadId)}`;
            xhr.open('GET', url, true);
            xhr.withCredentials = true;
            xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
            if (signal) {
                if (signal.aborted) {
                    done(new Set());
                    return;
                }
                signal.addEventListener('abort', () => {
                    try { xhr.abort(); } catch (err) { /* ignore */ }
                    done(new Set());
                }, { once: true });
            }
            xhr.onload = () => {
                if (xhr.status < 200 || xhr.status >= 300) {
                    done(new Set());
                    return;
                }
                let body = null;
                try {
                    body = xhr.responseText ? JSON.parse(xhr.responseText) : null;
                } catch (err) {
                    body = null;
                }
                if (body && body.exists && body.chunk_size === expectedChunkSize && Array.isArray(body.received_chunks)) {
                    done(new Set(body.received_chunks));
                    return;
                }
                done(new Set());
            };
            xhr.onerror = () => done(new Set());
            xhr.ontimeout = () => done(new Set());
            xhr.onabort = () => done(new Set());
            xhr.send();
        } catch (err) {
            done(new Set());
        }
    });
}

async function uploadChunkWithRetry(file, conversationId, uploadId, index, totalChunks, start, end, chunkBytes, chunkSize, contentType, options, onProgress) {
    for (let attempt = 0; attempt < UPLOAD_CHUNK_MAX_ATTEMPTS; attempt++) {
        if (attempt > 0) {
            await uploadSleep(UPLOAD_CHUNK_RETRY_DELAYS_MS[attempt - 1] + Math.random() * 250);
            if (options.signal && options.signal.aborted) {
                const error = new Error(AurvekI18n.t('chat_widgets.files.cancelled'));
                error.name = 'AbortError';
                throw error;
            }
            if (conversationId !== null && typeof currentConversationId !== 'undefined' && currentConversationId !== conversationId) {
                const error = new Error(AurvekI18n.t('chat_widgets.files.conversation_changed'));
                error.uploadFailed = true;
                error.uploadConversationChanged = true;
                throw error;
            }
        }

        const chunkBlob = file.slice(start, end);
        const formData = new FormData();
        formData.append('upload_id', uploadId);
        formData.append('chunk_index', String(index));
        formData.append('total_chunks', String(totalChunks));
        formData.append('filename', file.name || 'attachment');
        formData.append('content_type', contentType);
        formData.append('total_size', String(file.size));
        formData.append('chunk_size', String(chunkSize));
        formData.append('chunk', chunkBlob, `${file.name || 'attachment'}.part${index}`);

        const startedAt = performance.now();
        try {
            await postAttachmentForm(`/api/conversations/${conversationId}/attachments/chunk`, formData, {
                signal: options.signal,
                timeout: chunkTimeoutMs(chunkBytes),
                onProgress,
            });
            const elapsedSeconds = (performance.now() - startedAt) / 1000;
            recordChunkThroughput(chunkBytes, elapsedSeconds);
            return;
        } catch (error) {
            if (error.name === 'AbortError' || !error.uploadRetryable) {
                throw error;
            }
            if (attempt === UPLOAD_CHUNK_MAX_ATTEMPTS - 1) {
                recordChunkFailure();
                throw error;
            }
        }
    }
}

async function uploadSingleAttachment(file, conversationId, rendered, options = {}) {
    const contentType = getUploadContentType(file);
    const isResume = resumeRegistry.has(file) && resumeRegistry.get(file).conversationId === conversationId;
    let entry;
    if (isResume) {
        entry = resumeRegistry.get(file);
    } else {
        entry = {
            uploadId: createAttachmentUploadId(),
            chunkSize: pickAdaptiveChunkSize(),
            conversationId,
            failures: 0,
        };
        resumeRegistry.set(file, entry);
    }

    const totalChunks = Math.max(1, Math.ceil(file.size / entry.chunkSize));
    const received = isResume
        ? await queryUploadStatus(conversationId, entry.uploadId, entry.chunkSize, options.signal)
        : new Set();

    try {
        let bytesDone = 0;
        const sizeForPct = Math.max(1, file.size); // avoid NaN% for a 0-byte file
        for (let index = 0; index < totalChunks; index++) {
            if (conversationId !== null && typeof currentConversationId !== 'undefined' && currentConversationId !== conversationId) {
                const error = new Error(AurvekI18n.t('chat_widgets.files.conversation_changed'));
                error.uploadFailed = true;
                error.uploadConversationChanged = true;
                throw error;
            }
            if (options.signal && options.signal.aborted) {
                const error = new Error(AurvekI18n.t('chat_widgets.files.cancelled'));
                error.name = 'AbortError';
                throw error;
            }

            const start = index * entry.chunkSize;
            const end = Math.min(file.size, start + entry.chunkSize);
            const chunkBytes = end - start;

            if (received.has(index)) {
                bytesDone += chunkBytes;
                const overall = (bytesDone / sizeForPct) * 96;
                updateAttachmentUploadStatus(rendered, overall, `${Math.max(1, Math.round(overall))}%`);
                continue;
            }

            await uploadChunkWithRetry(
                file, conversationId, entry.uploadId, index, totalChunks,
                start, end, chunkBytes, entry.chunkSize, contentType, options,
                (loaded, total) => {
                    const chunkFraction = total > 0 ? loaded / total : 0;
                    const overall = ((bytesDone + chunkFraction * chunkBytes) / sizeForPct) * 96;
                    updateAttachmentUploadStatus(rendered, overall, `${Math.max(1, Math.round(overall))}%`);
                }
            );
            bytesDone += chunkBytes;
        }

        updateAttachmentUploadStatus(rendered, 98, () => AurvekI18n.t('chat_widgets.files.processing'));

        const completeForm = new FormData();
        completeForm.append('upload_id', entry.uploadId);
        completeForm.append('total_chunks', String(totalChunks));
        completeForm.append('filename', file.name || 'attachment');
        completeForm.append('content_type', contentType);
        completeForm.append('total_size', String(file.size));
        const completed = await postAttachmentForm(`/api/conversations/${conversationId}/attachments/complete`, completeForm, {
            signal: options.signal
        });

        resumeRegistry.delete(file);
        uploadProfile.recentFailures = 0;
        markAttachmentUploadComplete(rendered);
        return completed;
    } catch (err) {
        // Only genuine upload failures (network/timeout/5xx) count toward the
        // resume-then-shrink threshold. A user cancel (AbortError) or a
        // conversation switch is an intentional stop and must keep the resume
        // entry so a later re-send can continue cheaply.
        if (err.name !== 'AbortError' && !err.uploadConversationChanged) {
            entry.failures += 1;
            if (entry.failures >= UPLOAD_MAX_FULL_RETRIES_BEFORE_SHRINK) {
                resumeRegistry.delete(file);
            }
        }
        throw err;
    }
}

async function uploadAttachmentsForMessage(files, conversationId, displayOptions = {}) {
    const uploadBatch = Object.freeze(Array.from(files || []));
    const renderedAttachmentElements = [];
    renderedAttachmentElements.cancelled = false;
    renderedAttachmentElements.attachmentRefs = [];

    if (uploadBatch.length === 0) {
        return renderedAttachmentElements;
    }

    if (window.SessionManager && typeof window.SessionManager.validateSession === 'function') {
        const isValid = await window.SessionManager.validateSession(true);
        if (!isValid) {
            const error = new Error(AurvekI18n.t('common.session.expired_message'));
            error.code = 'session_expired';
            error.uploadFailed = true;
            throw error;
        }
    }

    for (const file of uploadBatch) {
        if (!isAcceptedFileType(file) || !validateFileSize(file)) {
            if (!isAcceptedFileType(file)) {
                NotificationModal.warning(() => AurvekI18n.t('chat_widgets.files.invalid_title'), () => AurvekI18n.t('chat_widgets.files.invalid_type'));
            }
            const error = new Error(AurvekI18n.t('chat_widgets.files.invalid_attachment'));
            error.uploadFailed = true;
            throw error;
        }

        const rendered = createAttachmentUploadEcho(file, conversationId, displayOptions);
        renderedAttachmentElements.push(rendered.element);
        try {
            const uploaded = await uploadSingleAttachment(file, conversationId, rendered, displayOptions);
            if (!uploaded || !uploaded.attachment_ref) {
                const error = new Error(AurvekI18n.t('chat_widgets.files.missing_reference'));
                error.uploadFailed = true;
                throw error;
            }
            renderedAttachmentElements.attachmentRefs.push(uploaded);
        } catch (error) {
            // A user cancel (AbortError) is not a failure: do not paint the echo
            // red or mark uploadFailed, so chat.js handles it via its quiet
            // AbortError branch. Still expose the echoes so that branch can
            // remove them and revoke their blob URLs.
            if (error.name !== 'AbortError') {
                markAttachmentUploadFailed(rendered, error.message || AurvekI18n.t('chat_widgets.files.failed'));
                error.uploadFailed = true;
            }
            error.renderedAttachmentElements = renderedAttachmentElements;
            throw error;
        }
    }

    return renderedAttachmentElements;
}

window.uploadAttachmentsForMessage = uploadAttachmentsForMessage;
window.discardUploadedAttachmentRefs = discardUploadedAttachmentRefs;
window.getAttachmentRefsFromUploadElements = getAttachmentRefsFromUploadElements;


function initFileHandling() {
    if (!Config.can_send_files) return;
    document.addEventListener('paste', handlePasteEvent);
    const fileInput = document.getElementById('chat-files');
    if (fileInput) fileInput.addEventListener('change', handleFileSelect);
}

async function handleFileSelect(event) {
    const files = event.target.files;
    const imagePreviews = document.getElementById('image-previews');

    compressionInProgress++;
    try {
        let compressedCount = 0;
        let totalSavedBytes = 0;

        for (const file of files) {
            if (!isAcceptedFileType(file) || !validateFileSize(file)) {
                if (!isAcceptedFileType(file)) {
                    NotificationModal.warning(() => AurvekI18n.t('chat_widgets.files.invalid_title'), () => AurvekI18n.t('chat_widgets.files.invalid_type'));
                }
                continue;
            }

            if (file.type === 'application/pdf') {
                // PDF preview (unchanged)
                const pdfPreview = document.createElement('div');
                pdfPreview.className = 'pdf-preview-item';
                const iconSpan = document.createElement('span');
                iconSpan.className = 'pdf-icon';
                iconSpan.textContent = 'PDF';
                const nameSpan = document.createElement('span');
                nameSpan.className = 'pdf-name';
                nameSpan.textContent = file.name;
                pdfPreview.appendChild(iconSpan);
                pdfPreview.appendChild(nameSpan);
                trackAttachmentPreview(file, pdfPreview);
                imagePreviews.appendChild(pdfPreview);
                attachedFiles.push(file);
            } else if (isTextFile(file)) {
                // Text preview (unchanged)
                const previewItem = document.createElement('div');
                previewItem.className = 'text-preview-item';
                const icon = document.createElement('span');
                icon.className = 'text-icon';
                icon.textContent = 'TXT';
                const name = document.createElement('span');
                name.className = 'text-name';
                name.textContent = file.name;
                previewItem.appendChild(icon);
                previewItem.appendChild(name);
                trackAttachmentPreview(file, previewItem);
                imagePreviews.appendChild(previewItem);
                attachedFiles.push(file);
            } else {
                // Image: compress before preview
                const processed = await maybeCompressImage(file);
                attachedFiles.push(processed);

                const reader = new FileReader();
                reader.onload = function (e) {
                    if (!attachmentIsStillQueued(processed)) return;
                    const img = document.createElement('img');
                    img.src = e.target.result;
                    img.className = 'preview-image';
                    trackAttachmentPreview(processed, img);
                    imagePreviews.appendChild(img);
                };
                reader.onerror = function (e) {
                    console.error('Error reading file:', e);
                };
                reader.readAsDataURL(processed);

                if (processed !== file && processed.size < file.size) {
                    compressedCount++;
                    totalSavedBytes += file.size - processed.size;
                }
            }
        }

        if (compressedCount > 0) {
            const saved = new Intl.NumberFormat(AurvekI18n.locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(totalSavedBytes / (1024 * 1024));
            const msg = AurvekI18n.t('chat_widgets.files.images_compressed', {count: compressedCount, saved});
            NotificationModal.toast(msg, 'info', 3000);
        }
    } finally {
        compressionInProgress--;
    }

    if (attachedFiles.length > 0) {
        imagePreviews.classList.remove('hidden');
    }
    document.getElementById('message-text').focus();
}
