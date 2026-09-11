const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {create} = require('../../data/static/js/common/i18n.js');
const html = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const tr = create(JSON.parse(html.match(/<script[^>]*id="aurvek-i18n"[^>]*>([\s\S]*?)<\/script>/)[1]));
const nodes = new Map();
const get = id => {if (!nodes.has(id)) nodes.set(id, {id, style: {}, value: '', checked: true, addEventListener(type, handler) {this[type] = handler;}});return nodes.get(id);};
const slider = get('tts_webchat_stability');slider.value = '0.45';
get('tts_webchat_model').value = 'eleven_v3';get('tts_webchat_format').value = 'mp3_44100_128';
const c = vm.createContext({AurvekI18n: tr, document: {getElementById: get, querySelectorAll: () => [slider]}});
for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
    new vm.Script(match[1]);
    if (match[1].includes('wsIncompatibleModels')) vm.runInContext(match[1], c);
}
slider.input();
assert.equal(get('tts_webchat_stability_val').textContent, tr.formatNumber(0.45, {maximumFractionDigits: 2}));
assert.equal(slider.value, '0.45');
assert.equal(get('warning_v3_ws').style.display, '');
assert.equal(get('warning_format_ws').style.display, 'none');
get('tts_webchat_format').value = 'opus_48000_128';get('tts_webchat_format').change();
assert.equal(get('warning_format_ws').style.display, '');
get('tts_webchat_ws_enabled').checked = false;get('tts_webchat_ws_enabled').change();
assert.equal(get('warning_v3_ws').style.display, 'none');
assert.equal(get('chunk_schedule_row').style.display, 'none');
