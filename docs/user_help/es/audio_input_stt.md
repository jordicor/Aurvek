---
id: audio_input_stt
title: "Envío de mensajes de voz con conversión de voz a texto"
category: chat
keywords:
  - "voz"
  - "micrófono"
  - "voz a texto"
  - "STT"
  - "entrada de audio"
  - "grabar"
  - "dictado"
  - "retranscribir"
  - "nota de voz"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: es
base_source_hash: e5f0b9351bfb8dc6d7d2c297e9ee4de62d51d62aee15a2132b13c582e426cc4c
---

## Respuesta breve

Puedes grabar un mensaje de voz con el micrófono. Aurvek lo transcribe automáticamente y lo envía como un mensaje de chat normal. Pulsa el botón del micrófono para empezar y luego envía o cancela.

## Pasos

1. Pulsa el **icono del micrófono** junto al campo de mensaje.
2. Si se solicita, permite al navegador acceder al micrófono.
3. El campo cambia al modo de grabación y muestra un temporizador y controles; el micrófono pasa a ser un icono de parada.
4. Dicta el mensaje.
5. Pulsa **enviar** (flecha) para detener y enviar el audio, o **cancelar** (X) para descartarlo.
6. El servidor transcribe el audio, coloca el texto en el campo y lo envía automáticamente al chat.

## Notas

- El navegador debe admitir MediaRecorder; la mayoría de los navegadores modernos lo hacen.
- Debes permitir el micrófono. Si lo denegaste, cambia los permisos del sitio en el navegador.
- La grabación usa WebM con códec Opus.
- La transcripción consume saldo. Si no hay suficiente, recibirás un aviso.
- Una grabación corta o silenciosa puede producir una transcripción vacía (respuesta 204) y no se enviará ningún mensaje.
- Las notas de voz de WhatsApp y Telegram siguen el flujo de canales externos. Si se conservó el audio original, puedes pedir otra transcripción desde el mensaje, revisarla y aceptarla antes de sustituir el texto visible.

## Relacionado

- tts_listen_messages
- chat_search
- external_platforms_overview
