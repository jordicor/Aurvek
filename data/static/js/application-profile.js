/* Account-only frame. Personal fields never cross postMessage. */
(function () {
    'use strict';
    const meta = JSON.parse(document.getElementById('profile-metadata').textContent);
    const base = location.pathname;
    const field = id => document.getElementById(id);
    let current, operation, phoneProof, phoneOperation, saving = false;
    function text(key) {
        const [domain, ...parts] = key.split('.');
        return meta.messages.resources?.[meta.ui_language]?.[domain]?.[parts.join('.')]
            || meta.messages.resources?.en?.[domain]?.[parts.join('.')] || key;
    }
    function emit(event, data = {}) {
        parent.postMessage({contract_version: 'aurvek_embed.v1', surface: 'profile', app_id: meta.app_id,
            frame_instance_id: meta.frame_instance_id, event, ...data}, meta.parent_origin);
    }
    async function api(path, body) {
        const response = await fetch(base + '/' + path, {method: body ? 'POST' : 'GET', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-GPTSub-CSRF': meta.csrf_token},
            ...(body ? {body: JSON.stringify(body)} : {})});
        const data = await response.json();
        if (!response.ok) throw Object.assign(new Error(data.error || 'profile_error'), {code: data.error || 'profile_error'});
        return data;
    }
    function render(state, hydrate = true) {
        if (!current) field('phone-editor').open = !state.contact.verified;
        current = state;
        if (hydrate) {
            for (const key of ['preferred_name', 'timezone_name', 'about_me']) field(key).value = state.profile[key] || '';
            field('primary_language').value = state.profile.preferred_languages[0] || '';
            for (const option of field('additional_languages').options) option.selected = state.profile.preferred_languages.slice(1).includes(option.value);
        }
        field('contact').textContent = state.contact.phone_number || '—';
        field('phone-editor').hidden = false;
        field('phone-action').textContent = text(state.contact.verified ? 'application.profile_change_phone' : 'application.profile_verify_phone');
        const channelNames = {phone: text('profile.calls.direction.phone'), whatsapp: 'WhatsApp', telegram: 'Telegram'};
        field('channels').replaceChildren(...state.channels.map(channel => {
            const item = document.createElement('li'); item.textContent = channelNames[channel.channel] + ': ' + text('application.profile_' + channel.status);
            for (const receiver of channel.receivers) {
                if (!receiver.connected && (!state.contact.verified || channel.channel === 'telegram')) continue;
                const button = document.createElement('button'); button.type = 'button';
                button.textContent = text(receiver.connected ? 'profile.subscription.disconnect' : 'application.profile_connect');
                button.onclick = async () => {
                    button.disabled = true;
                    try {render(await api('channel', {receiver_id: receiver.receiver_id,
                        action: receiver.connected ? 'disconnect' : 'connect', expected_version: receiver.version}), false);
                        emit('profile_saved', {version: current.version});
                    } catch (err) {error(err);} finally {button.disabled = false;}
                };
                item.append(' ', button);
            }
            return item;
        }));
        emit('resize', {height: document.documentElement.scrollHeight});
    }
    function error(err) { field('status').textContent = text('profile.error.profile_update'); field('status').dataset.error = 'true'; emit('error', {code: err.code || 'profile_error'}); }
    field('phone-editor').ontoggle = () => emit('resize', {height: document.documentElement.scrollHeight});
    field('device-zone').onclick = () => { field('timezone_name').value = Intl.DateTimeFormat().resolvedOptions().timeZone; };
    field('request-phone').onclick = async () => {
        if (saving) return; saving = true; field('request-phone').disabled = true;
        try {
            const phone_number = field('phone_number').value;
            phoneProof = {...await api('phone', {action: 'request', phone_number}), phone_number};
            phoneOperation = null; field('status').textContent = '✓';
        } catch (err) {error(err);} finally {saving = false; field('request-phone').disabled = false;}
    };
    field('phone-form').onsubmit = async event => {
        event.preventDefault(); if (saving || !current || !phoneProof || phoneProof.phone_number !== field('phone_number').value) return;
        saving = true; field('verify-phone').disabled = true;
        try {
            const values = {phone_number: phoneProof.phone_number, challenge_id: phoneProof.challenge_id};
            if (!phoneOperation) {
                await api('phone', {action: 'verify', ...values, code: field('phone_code').value});
                phoneOperation = {action: 'update', ...values, operation_id: crypto.randomUUID(),
                    expected_version: current.version, expected_contact_version: current.contact.version};
            }
            render(await api('phone', phoneOperation), false); phoneProof = null; phoneOperation = null;
            field('phone-editor').open = false;
            field('phone_code').value = ''; emit('profile_saved', {version: current.version});
        } catch (err) {error(err);} finally {saving = false; field('verify-phone').disabled = false;}
    };
    field('profile-form').onsubmit = async event => {
        event.preventDefault(); if (saving || !current) return;
        const profile = {schema_version: 1, preferred_name: field('preferred_name').value || null,
            about_me: field('about_me').value || null, timezone_name: field('timezone_name').value || null,
            preferred_languages: [...new Set([field('primary_language').value, ...Array.from(field('additional_languages').selectedOptions, item => item.value)].filter(Boolean))]};
        const signature = JSON.stringify(profile);
        if (!operation || operation.signature !== signature) operation = {signature, operation_id: crypto.randomUUID(), expected_version: current.version, profile};
        saving = true; field('save').disabled = true;
        try {
            const state = await api('save', {operation_id: operation.operation_id, expected_version: operation.expected_version, profile});
            render(state); operation = null; field('status').textContent = '✓'; field('status').dataset.error = 'false';
            emit('profile_saved', {version: state.version});
        } catch (err) { error(err); if (err.code === 'version_conflict') { current = await api('state'); operation = null; } }
        finally { saving = false; field('save').disabled = false; }
    };
    addEventListener('message', async event => {
        if (event.source !== parent || event.origin !== meta.parent_origin) return;
        const data = event.data;
        if (!data || data.contract_version !== 'aurvek_embed.v1' || data.surface !== 'profile' || data.app_id !== meta.app_id || data.frame_instance_id !== meta.frame_instance_id) return;
        if (data.event !== 'set_ui_language' || typeof data.request_id !== 'string' || data.request_id.length > 128 || !['en','es','ja','fr','pt','it','de'].includes(data.ui_language)) return;
        try {
            const result = await api('language', {ui_language: data.ui_language});
            meta.ui_language = result.ui_language; meta.messages = result.messages;
            document.documentElement.lang = result.ui_language;
            // Relabel the existing options: values, selections and drafts stay intact.
            const languages = new Map(result.languages.map(item => [item.code, item.label]));
            for (const id of ['primary_language', 'additional_languages']) {
                for (const option of field(id).options) {
                    if (languages.has(option.value)) option.textContent = languages.get(option.value);
                }
            }
            for (const element of document.querySelectorAll('[data-i18n]')) {
                const [domain, ...parts] = element.dataset.i18n.split('.');
                const value = result.messages.resources?.[result.ui_language]?.[domain]?.[parts.join('.')]
                    || result.messages.resources?.en?.[domain]?.[parts.join('.')];
                if (typeof value === 'string') element.textContent = value;
            }
            if (current) render(current, false);
            emit('language_changed', {ui_language: result.ui_language, request_id: data.request_id});
        } catch (err) { error(err); }
    });
    api('state').then(state => { render(state); emit('ready', {version: state.version}); }).catch(error);
})();
