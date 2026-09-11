const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const {create} = require('../../data/static/js/common/i18n.js');
const html = fs.readFileSync(0, 'utf8');
const payload = JSON.parse(html.match(/<script[^>]*id="aurvek-i18n"[^>]*>([\s\S]*?)<\/script>/)[1]);
const tr = create(payload);
const esc = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
class Element {
    constructor() { this.value = ''; this.style = {}; this.dataset = {}; this.children = []; this.options = []; this.classList = {add(){}, remove(){}, replace(){}}; }
    set textContent(value) { this.text = String(value); this.innerHTML = esc(value); }
    get textContent() { return this.text; }
    addEventListener() {}
    appendChild(child) { this.children.push(child); }
    querySelectorAll() { return []; }
    querySelector() { return new Element(); }
    insertAdjacentHTML(_, html) { this.innerHTML = html; }
}
const elements = new Map();
const get = id => {if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id);};
const notices = [];
const c = vm.createContext({AurvekI18n: {...tr, t: tr.render},
    document: {getElementById: get, querySelector: get, querySelectorAll: () => [], createElement: () => new Element(), addEventListener() {}},
    console, setTimeout(){}, clearTimeout(){}, setInterval(){}, clearInterval(){},
    bootstrap: {Modal: class {show(){} static getInstance(){return null;}}},
    NotificationModal: {warning: (...args) => notices.push(args), confirm: (...args) => notices.push(args)},
});
c.window = c;
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
vm.runInContext(script, c);
get('markupInput').value = '1234.56789';
c.updateEarningsPreview();
assert.equal(get('previewEarnings').textContent, tr.formatCurrency(1234.56789 * 0.7));
assert.equal(get('markupInput').value, '1234.56789');
c.setupVoiceControls();
assert.equal(get('sampleCategory').children[0].textContent, tr.render('prompt_editor.runtime.children_and_basic_education'));
assert.equal(get('sampleCategory').children[0].value, 0);
for (const count of [1, 2]) {
    c.showWatchdogSuggestionModal({mode:'custom', objectives:['Creator <goal>'], steering_prompt:'Creator instruction', frequency:count, max_hint_chars:1250, thresholds:{max_turns_off_topic:3,max_turns_same_subtopic:5}});
    const body = get('wdSuggestionBody').innerHTML;
    assert.ok(body.includes(esc(tr.render('prompt_editor.watchdog.frequency', {count, turns:tr.formatNumber(count)}))));
    assert.ok(body.includes('Creator &lt;goal&gt;'));
}
if (process.argv[2] === 'edit') {
    get('editorSelect').value = '73';
    get('editorSelect').options = [{text: 'Original <editor>'}];
    get('editorSelect').selectedIndex = 0;
    c.addEditor();
    assert.ok(get('.editor-list').children.at(-1).innerHTML.includes(esc(tr.render('prompt_editor.text.remove'))));
    assert.ok(get('.editor-list').children.at(-1).innerHTML.includes('Original &lt;editor&gt;'));
    c.deleteExtension(4, 'Creator " <name>');
    assert.equal(notices.pop()[1], tr.render('prompt_editor.extension.delete_confirm', {name:'Creator " <name>'}));
    c.addGranSabioLayer();
    assert.ok(get('gsQaLayers').innerHTML.includes(esc(tr.render('prompt_editor.qa.layer', {number:'1'}))));
    c.renderExtensionsList([{id:4,name:'Creator " <name>',description:'Original <description>',is_default:true}]);
    assert.ok(get('extensions-list').innerHTML.includes('data-extension-name="Creator &quot; &lt;name&gt;"'));
}
