---
id: audio_input_stt
title: Sending voice messages with speech-to-text
category: chat
keywords:
  - voice
  - microphone
  - speech to text
  - stt
  - audio input
  - grabar
  - microfono
  - voz
  - dictado
  - retranscribe
  - voice note
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
---

## Short answer

You can use your microphone to record a voice message, which is automatically transcribed to text and sent as a regular chat message. Click the microphone button to start recording, then send or cancel.

## Steps

1. Click the **microphone icon** next to the message input area.
2. If prompted, grant your browser permission to access the microphone.
3. The input area switches to recording mode, showing a timer and recording controls. The microphone icon changes to a stop icon.
4. Speak your message.
5. Click the **send button** (arrow icon) to stop recording and send the audio for transcription. Alternatively, click the **cancel button** (X icon) to discard the recording.
6. After sending, the audio is transcribed on the server. The transcribed text is placed in the message input and automatically sent as a chat message.

## Notes

- Your browser must support audio recording (MediaRecorder API). Most modern browsers do.
- You need to allow microphone access when your browser requests it. If you denied it previously, you will need to update your browser's site permissions.
- The recording format is WebM with Opus codec.
- Transcription uses your account balance. If you have insufficient balance, you will receive a notification.
- Short or silent recordings may result in an empty transcription (204 response), and no message will be sent.
- WhatsApp and Telegram voice notes use the external-channel flow. If their original audio was retained, you can later request a new transcription from the message, review it, and accept it before it replaces the visible text.

## Related

- tts_listen_messages
- chat_search
- external_platforms_overview
