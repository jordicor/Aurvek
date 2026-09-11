---
id: settings_language
title: Interface and conversation languages
category: settings
keywords:
  - interface language
  - conversation language
  - change language
  - translation
  - menus
  - preferred language
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-08
---

## Short answer

**Interface language** changes menus, forms, and platform notices. Choose it from the account menu's **Language** submenu, beside **Theme**. It is separate from **Primary conversation language**, which helps the assistant and speech recognition when the conversation does not already establish a language.

## Steps

1. Open the account menu and choose **Language**, beside **Theme**.
2. Choose English, Spanish, Japanese, French, Portuguese, Italian, or German. Language names appear in their own language.
3. The choice saves immediately as your account UI preference and the **Settings > Profile** selector updates immediately too. A safe page reload applies every label.
4. If the page has unsaved changes, they stay in place until you allow the reload. In a chat, finish a call, recording, or reply first, or clear or save a draft and attachments before reloading.
5. You can still use **Settings > Profile** to choose the interface language. If needed, set **Primary conversation language** and other languages there, then save. Leave the primary language blank for automatic detection.

## Notes

- Changing the interface does not translate existing conversations or change the assistant's voice.
- An explicit language rule in the assistant's instructions takes priority. Otherwise, your current request or conversation establishes the reply language; your primary conversation language is the fallback.
- In a chat embedded in another application, that application controls the interface language. Use its language setting when available; the embedded chat does not need a second selector.
- Platform help follows the conversation language. If current help is only available in English, the assistant can explain it in the conversation language.

## Related

- settings_profile
- audio_input_stt
- tts_listen_messages
