/* Shared entry points for native controls and authenticated application hosts. */
(function (global) {
    'use strict';
    let exporting = null;
    const error = code => Object.assign(new Error(code), { code });
    const tr = key => global.AurvekI18n.t(key);
    function scope() {
        const embed = global.AurvekEmbed;
        const native = global.applicationConversation;
        const id = global.currentConversationId;
        if (embed?.config.application) return { id: embed.config.conversation_id, caps: embed.config.capabilities, controls: embed.config.controls };
        if (native && String(native.id) === String(id)) return { id, caps: native.application.capabilities };
        return null;
    }
    function panel() {
        let node = document.getElementById('application-action-controls');
        if (!node) {
            node = document.createElement('div'); node.id = 'application-action-controls';
            node.setAttribute('role', 'status'); node.setAttribute('aria-live', 'polite');
            document.getElementById('form-message').before(node);
        }
        return node;
    }
    function button(label, action) {
        const node = document.createElement('button'); node.type = 'button'; node.className = 'btn btn-sm btn-outline-secondary';
        node.textContent = label; node.addEventListener('click', action); return node;
    }
    function report(errorValue) {
        panel().textContent = tr('chat.error');
        if (exporting) panel().append(button(tr('chat_ui.action.cancel'), () => { void stopExport().catch(report); }));
        global.AurvekEmbed?.emit('error', { code: errorValue.code || 'action_failed' });
    }
    async function fetchJson(url, options = {}) {
        const token = global.AurvekEmbed?.config.csrf_token || document.querySelector('meta[name="aurvek-csrf-token"]')?.content;
        const headers = new Headers(options.headers || {});
        if (token) headers.set('X-GPTSub-CSRF', token);
        const response = await (global.secureFetch || global.fetch).call(global, url, { ...options, headers });
        if (!response?.ok) {
            const failure = await response?.json().catch(() => ({}));
            const code = failure?.error || failure?.detail;
            throw error(typeof code === 'string' ? code : 'export_failed');
        }
        return response.json();
    }
    function exportPath(value, job, content = false) {
        const url = new URL(value, global.location.origin);
        const expected = `/api/conversations/${job.id}/exports/${job.kind}/${job.jobId}${content ? '/content' : ''}`;
        if (url.origin !== global.location.origin || url.pathname !== expected || url.search || url.hash) throw error('invalid_export_response');
        return url.pathname;
    }
    async function watchExport(job) {
        // The server expires jobs after its bounded queue/worker timeout. Keep
        // that single clock authoritative instead of losing a still-running job.
        while (true) {
            const result = await fetchJson(job.statusUrl, { cache: 'no-store' });
            if (result.status === 'completed') {
                job.finished = true;
                const path = exportPath(result.download_url, job, true);
                const href = global.AurvekEmbed ? global.AurvekEmbed.resourceUrl(path) : path;
                if (!href) throw error('invalid_export_response');
                const link = document.createElement('a'); link.href = href; link.download = `conversation-${job.id}.${job.kind}`;
                link.textContent = tr('chat_ui.action.download'); link.className = 'btn btn-sm btn-primary';
                if (String(scope()?.id) === String(job.id)) panel().replaceChildren(link);
                global.AurvekEmbed?.emit('export_completed', { format: job.kind });
                return;
            }
            if (result.status === 'cancelled') { job.finished = true; panel().replaceChildren(); return; }
            if (result.status === 'failed') { job.finished = true; throw error(result.error || 'export_failed'); }
            if (!['queued', 'running', 'ending'].includes(result.status)) throw error('invalid_export_response');
            await new Promise(resolve => global.setTimeout(resolve, 1000));
        }
    }
    async function startExport(kind, current) {
        if (exporting) throw error('operation_in_progress');
        const job = { id: current.id, kind, statusUrl: null, done: null };
        exporting = job;
        try {
            const result = await fetchJson(`/api/conversations/${current.id}/exports/${kind}`, { method: 'POST' });
            if (!/^[A-Za-z0-9_-]{32}$/.test(result.job_id)) throw error('invalid_export_response');
            job.jobId = result.job_id;
            job.statusUrl = exportPath(result.status_url, job);
            const progress = document.createElement('span'); progress.textContent = tr('chat.processing') + ' ';
            panel().replaceChildren(progress, button(tr('chat_ui.action.cancel'), () => { void stopExport().catch(report); }));
            job.done = watchExport(job).finally(() => { if (exporting === job && job.finished) exporting = null; });
            job.done.catch(report);
            return { status: 'started' };
        } catch (value) { if (exporting === job) exporting = null; throw value; }
    }
    async function stopExport() {
        const job = exporting;
        if (!job) return;
        // Starting is short, but the stop must not acknowledge before the job exists.
        if (!job.statusUrl) throw error('activity_close_pending');
        await fetchJson(job.statusUrl + '/stop', { method: 'POST' });
        let timer;
        try {
            const settled = job.done.catch(async () => {
                if (job.finished) return;
                // A failed status request is not a completed export. After the
                // explicit stop, resume the same job rather than starting one.
                try { await watchExport(job); }
                finally { if (exporting === job && job.finished) exporting = null; }
            });
            await Promise.race([settled, new Promise((_, reject) => {
                timer = global.setTimeout(() => reject(error('activity_close_pending')), 30000);
            })]);
        } finally { global.clearTimeout(timer); }
    }
    async function run(name, { host = false } = {}) {
        const current = scope();
        const cap = { attachment: 'attachments', dictation: 'stt', voice: 'voice', export_pdf: 'export_pdf', export_mp3: 'export_mp3' }[name];
        if (!cap || (current && (!current.caps?.[cap] || current.controls?.[cap] === false)) ||
            (name === 'export_mp3' && current && !current.caps.tts)) throw error('capability_disabled');
        if (global.AurvekEmbed?.isExpired()) throw error('session_expired');
        global.closePlusMenu?.();
        if (name === 'attachment') {
            const input = document.getElementById('chat-files');
            if (!input || input.disabled) throw error('action_unavailable');
            // postMessage does not carry the host's transient user activation.
            if (host) {
                const choose = button(tr('chat_ui.tools.attach_files'), () => { input.click(); panel().replaceChildren(); });
                panel().replaceChildren(choose); choose.focus();
                return { status: 'interaction_required' };
            }
            input.click();
        } else if (name === 'dictation') {
            await global.toggleAudioRecording();
        } else if (name === 'voice') {
            if (!global.AurvekVoice?.open) throw error('action_unavailable');
            await global.AurvekVoice.open();
        } else if (current) {
            return startExport(name === 'export_pdf' ? 'pdf' : 'mp3', current);
        } else {
            (name === 'export_pdf' ? global.downloadPDF : global.downloadAudio)(global.currentConversationId);
        }
        return { status: 'opened' };
    }
    const fromControl = name => { void run(name).catch(report); };
    global.AurvekChatActions = Object.freeze({ run, fromControl, stopExport, activity: () => exporting ? 'generating' : 'idle' });
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[data-application-action]').forEach(node => {
            node.addEventListener('click', () => fromControl(node.dataset.applicationAction));
        });
    });
})(window);
