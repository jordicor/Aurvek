---
id: chat_export_mp3
title: "Exportación de una conversación a audio (MP3)"
category: chat
keywords:
  - "MP3"
  - "audio"
  - "exportar"
  - "descargar"
  - "texto a voz"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-11
locale: es
base_source_hash: b3c1ef1899fa03562f45b2774ce0b7d2121a2ed97f46e036e5ae1b9968a2ac6d
---

## Respuesta breve

Puedes exportar toda una conversación a MP3. Cada llamada terminada usa una vez su grabación continua, conservando ambas voces, pausas y voces simultáneas. Los demás audios conservados, incluidas las notas de WhatsApp o Telegram, mantienen su orden. Solo los mensajes sin audio utilizable necesitan síntesis de voz (TTS).

## Pasos

1. Abre el menú de tres puntos de la conversación en la barra lateral.
2. Elige **Descargar MP3** y confirma.
3. Aurvek pone la generación en segundo plano y muestra un aviso.
4. Al terminar, abre la **Galería multimedia** para descargar el MP3.

## Notas

- La generación puede tardar según la longitud del chat.
- El audio conservado mantiene las voces originales. Los mensajes sin audio usan las voces configuradas del prompt y de la cuenta.
- Se usa la llamada completa cuando pertenece íntegramente a esta conversación y sus mensajes forman un bloque consecutivo. En otros casos, o si falta esa grabación, se usan los audios individuales disponibles de cada mensaje.
- Las grabaciones individuales excepcionalmente largas pueden usar TTS para que la exportación sea fiable.
- El nombre incluye el prompt y una marca de tiempo, por ejemplo `My_Prompt_2026_03_22_14_30_00.mp3`.
- Si ya hay una generación en curso, espera unos minutos antes de pedir otra para la misma conversación.
- Solo se descuenta saldo por las partes que necesitan TTS; reutilizar audio original no añade ese coste.

## Relacionado

- chat_export_pdf
- tts_listen_messages
