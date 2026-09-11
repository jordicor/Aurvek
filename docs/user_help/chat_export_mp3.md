---
id: chat_export_mp3
title: Exporting a conversation to audio (MP3)
category: chat
keywords:
  - mp3
  - audio
  - export
  - download
  - descargar
  - exportar audio
  - text to speech
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-11
---

## Short answer

You can export an entire conversation as an MP3. Completed phone calls use their saved continuous recording once, preserving both voices, pauses and simultaneous speech. Other retained audio, including WhatsApp/Telegram voice notes, stays in conversation order. Only messages without usable audio need text-to-speech (TTS).

## Steps

1. In the chat sidebar, click the three-dot menu on the conversation you want to export.
2. Select **Download MP3**.
3. Confirm the action in the dialog that appears.
4. The system queues the MP3 generation in the background. A message confirms it has started.
5. Once generation is complete, open the **Media Gallery** to find and download the MP3 file.

## Notes

- MP3 generation runs as a background task and may take some time depending on the length of the conversation.
- Retained phone audio and messaging voice notes keep their original voices. Bot messages and user messages without retained audio use the configured prompt and account voices.
- Whole-call audio is used when the call belongs entirely to this conversation and its messages form one consecutive block. Otherwise, or if that recording is unavailable, the export uses individual message audio where available.
- Exceptionally long individual recordings may use TTS fallback to keep background exports reliable.
- The generated file is named using the prompt name and a timestamp (e.g., `My_Prompt_2026_03_22_14_30_00.mp3`).
- If a generation is already in progress, you will need to wait a few minutes before requesting another one for the same conversation.
- MP3 export uses your account balance only for the parts that require TTS. Reusing retained original audio does not add a TTS charge.

## Related

- chat_export_pdf
- tts_listen_messages
