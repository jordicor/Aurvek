---
id: whatsapp_commands
title: "Comandos disponibles en WhatsApp"
category: whatsapp
keywords:
  - "comandos de WhatsApp"
  - "ayuda de WhatsApp"
  - "modo texto"
  - "modo voz"
  - "cambiar prompt"
  - "nueva conversación"
  - "listar conversaciones"
  - "cambiar conversación"
prerequisites:
  - "Una conversación de WhatsApp activa asignada a tu cuenta"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: es
base_source_hash: a88730b3fdf5473fa22b95fecca28ab8a93e10bf1b964484b89d9d9bd31db2df
---

## Respuesta breve

WhatsApp admite comandos que empiezan por `!` y no distinguen mayúsculas para controlar la conversación sin salir del chat.

## Pasos

1. `!help`: muestra los comandos.
2. `!text`: responde con texto; también `text mode` o `text_mode`.
3. `!voice`: responde con audio; también `voice mode` o `voice_mode`.
4. `!prompt list`: lista hasta 20 prompts con ID y nombre.
5. `!prompt <name or id>`: cambia el prompt por ID numérico o nombre parcial, por ejemplo `!prompt 42` o `!prompt email campaigns`.
6. `!new`: crea una conversación y guarda la anterior.
7. `!chats`: lista conversaciones con ID, título, mensajes, última actividad e insignias; `->` marca la activa.
8. `!set <id> [platform]`: cambia la activa. `!set 1234` asigna a WhatsApp y `!set 1234 telegram` la mueve a Telegram. Admite `wa`, `tg` e ID con o sin `#`.

## Notas

- Los comandos se procesan antes que la IA y no se le envían.
- `!prompt` prueba ID exacto, nombre exacto sin distinguir mayúsculas y nombre parcial.
- Si el chat está bloqueado, usa `!new`.
- El modo voz usa TTS y puede consumir saldo adicional según el plan.

## Relacionado

- whatsapp_continue_conversation
- whatsapp_setup_phone
- external_manage_conversations
