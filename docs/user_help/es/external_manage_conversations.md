---
id: external_manage_conversations
title: "Gestión de conversaciones desde WhatsApp o Telegram"
category: chat
keywords:
  - "listar conversaciones"
  - "cambiar conversación"
  - "comando chats"
  - "comando set"
  - "gestionar chats"
  - "asignar conversación"
prerequisites:
  - "Una cuenta de WhatsApp o Telegram vinculada a Aurvek"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: es
base_source_hash: 5b8b56741a26a97b05c721945c2e88b9d8ecb91eb691ee1bddc106b8d5e9da65
---

## Respuesta breve

Puedes listar y cambiar conversaciones de mensajería desde WhatsApp o Telegram con `!chats` y `!set`. La asignación telefónica se gestiona aparte y estos comandos no la modifican.

## Pasos

1. Envía `!chats` para ver conversaciones recientes con ID, título, número de mensajes, última actividad y plataforma. `->` marca la activa.
2. Envía `!set <id>` para asignar una conversación a la plataforma desde la que escribes, por ejemplo `!set 1234`.
3. Para moverla, envía `!set 1234 telegram` desde WhatsApp o `!set 1234 whatsapp` desde Telegram. También funcionan `tg` y `wa`.
4. Tu siguiente mensaje irá a la conversación elegida. La anterior no se elimina; solo pierde la vinculación con esa plataforma.

## Notas

- Solo puede haber una conversación activa por plataforma.
- Una conversación solo puede estar en WhatsApp o Telegram a la vez; su asignación telefónica independiente puede continuar.
- Si la mueves fuera de la plataforma actual, un aviso indica que el siguiente mensaje allí creará una conversación.
- Las conversaciones bloqueadas no se pueden asignar; usa `!new`.
- Para asignar a otra plataforma, esta debe estar vinculada a tu cuenta.
- También puedes asignar desde la barra lateral web.
- Usa **+ > Llámame** para gestionar el canal telefónico.

## Relacionado

- whatsapp_commands
- telegram_commands
- whatsapp_continue_conversation
- external_platforms_overview
- phone_calls_usage
