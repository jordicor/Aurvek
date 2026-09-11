---
id: tts_listen_messages
title: "Escuchar mensajes de IA con texto a voz"
category: chat
keywords:
  - "TTS"
  - "texto a voz"
  - "escuchar"
  - "audio"
  - "reproducir mensaje"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: es
base_source_hash: 0f5bb5a58de885d9c9b89a64102fbab81940cd0d83c3f0ed8279e5dd773e04c7
---

## Respuesta breve

Pulsa el altavoz de cualquier mensaje para convertir su texto en voz y transmitirla al navegador. Si ya se reprodujo antes, se carga al instante desde la caché.

## Pasos

1. Pasa el puntero sobre un mensaje y pulsa el **icono del altavoz**.
2. Se convierte en reloj de arena durante la carga.
3. Al reproducir, pasa a **detener**; púlsalo para parar.
4. Si pulsas otro mensaje, el audio actual se detiene.

## Notas

- Aurvek comprueba primero la caché y reproduce al instante si existe.
- Si no, genera y transmite por WebSocket para empezar antes de completar todo el mensaje.
- Generar TTS consume saldo; repetir desde caché es gratis.
- Sin saldo suficiente aparece un aviso.
- Se pueden escuchar mensajes del usuario y del bot; la voz depende del autor y del prompt.

## Relacionado

- audio_input_stt
- chat_export_mp3
