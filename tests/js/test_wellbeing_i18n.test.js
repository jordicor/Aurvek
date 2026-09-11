const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');
const source = fs.readFileSync(
    path.join(repoRoot, 'data/static/js/chat/wellbeing.js'),
    'utf8'
);
const { create: createI18n } = require('../../data/static/js/common/i18n.js');

function extract(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert.notEqual(start, -1, `Missing marker: ${startMarker}`);
    assert.notEqual(end, -1, `Missing marker: ${endMarker}`);
    return source.slice(start, end);
}

function runtime(language) {
    const catalog = JSON.parse(fs.readFileSync(
        path.join(repoRoot, 'locales', language, 'chat_widgets.json'),
        'utf8'
    ));
    const englishCatalog = JSON.parse(fs.readFileSync(
        path.join(repoRoot, 'locales', 'en', 'chat_widgets.json'),
        'utf8'
    ));
    const AurvekI18n = createI18n({
        version: 1,
        language,
        locales: {en: 'en-US', [language]: language},
        resources: {
            en: {chat_widgets: englishCatalog},
            [language]: {chat_widgets: catalog},
        },
    });
    const context = {AurvekI18n};
    vm.createContext(context);
    vm.runInContext(
        `${extract('function reminderText(', 'async function maybeShowReminder(')}
         globalThis.renderReminder = reminderText;`,
        context
    );
    return context;
}

test('built-in wellbeing notice keys render through the active locale', () => {
    const context = runtime('es');
    assert.equal(
        context.renderReminder({text_key: 'notice_soft', text: 'English backend default'}),
        'Llevas un tiempo usando el chat de forma continua. Considera hacer un breve descanso.'
    );
    assert.equal(
        context.renderReminder({text_key: 'notice_strong', text: 'English backend default'}),
        'Has tenido mucha actividad continua. Te recomendamos parar unos minutos antes de continuar.'
    );
});

test('administrator-configured wellbeing notice text is preserved byte-for-byte', () => {
    const context = runtime('en');
    const configured = 'Admin copy — keep spacing  exactly.';
    assert.equal(
        context.renderReminder({text_source: 'admin_configured', text: configured}),
        configured
    );
});

test('all locale catalogs contain wellbeing modal controls and default notices', () => {
    for (const language of fs.readdirSync(path.join(repoRoot, 'locales'))) {
        const catalog = JSON.parse(fs.readFileSync(
            path.join(repoRoot, 'locales', language, 'chat_widgets.json'),
            'utf8'
        ));
        for (const key of [
            'wellbeing.continue',
            'wellbeing.notice_soft',
            'wellbeing.notice_intense',
            'wellbeing.notice_strong',
            'wellbeing.pause_minutes',
        ]) {
            assert.ok(catalog[key], `${language} is missing ${key}`);
        }
    }
});
