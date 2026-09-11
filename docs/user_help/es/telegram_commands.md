---
id: telegram_commands
title: "Comandos del bot de Telegram"
category: telegram
keywords:
  - "Telegram"
  - "comandos"
  - "bot"
  - "ayuda"
  - "texto"
  - "voz"
  - "prompt"
  - "nueva conversación"
  - "desvincular"
  - "listar"
  - "cambiar conversación"
prerequisites:
  - "Una cuenta de Telegram vinculada a Aurvek (consulta telegram_setup)"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: es
base_source_hash: 90283b72cdd9c9a04f67c402858c03345ac26776501d8a38c9552a6734298ec1
---

## Respuesta breve

El bot de Telegram admite comandos con `!` para cambiar entre texto y voz, elegir prompt, listar o cambiar conversaciones, crear una o desvincular la cuenta. Escribe `!help` para verlos.

## Pasos

1. `!help`: muestra los comandos.
2. `!text`: usa respuestas de texto.
3. `!voice`: usa audio TTS y vuelve a texto si falla.
4. `!prompt list`: lista hasta 20 prompts con ID y nombre.
5. `!prompt <name or id>`: cambia el prompt por ID o nombre, con coincidencia parcial.
6. `!new`: crea una conversación y guarda la anterior.
7. `!unlink`: desvincula Telegram hasta que vuelvas a enlazarlo.
8. `!chats`: lista hasta 15 conversaciones con ID, título, mensajes y última actividad; `->` marca la activa.
9. `!set <id> [platform]`: cambia la activa. `!set 1234` asigna a Telegram y `!set 1234 whatsapp` a WhatsApp. Admite `wa`, `tg` y el prefijo opcional `#`.

## Notas

- No distingue mayúsculas: `!Help`, `!HELP` y `!help` funcionan.
- También admite texto, voz y fotos; la voz se transcribe automáticamente.
- Si el chat está bloqueado, usa `!new`.
- Las respuestas largas se dividen según el límite de Telegram.
- `!prompt` prueba ID exacto, nombre exacto y luego nombre parcial.

## Relacionado

- telegram_setup
- telegram_unlink
- external_manage_conversations
