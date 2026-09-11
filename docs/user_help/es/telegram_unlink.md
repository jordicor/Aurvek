---
id: telegram_unlink
title: "Cómo desvincular Telegram de tu cuenta de Aurvek"
category: telegram
keywords:
  - "Telegram"
  - "desvincular"
  - "desconectar"
  - "eliminar vínculo"
prerequisites:
  - "Una cuenta de Telegram vinculada actualmente a Aurvek (consulta telegram_setup)"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: es
base_source_hash: 856229a852405b67ff49f83d0bfc50d35d10122514120cb535d0884030d4a2a5
---

## Respuesta breve

Envía `!unlink` al bot de Aurvek en Telegram para eliminar inmediatamente el vínculo entre ambas cuentas.

## Pasos

1. Abre el chat con el bot.
2. Envía `!unlink`.
3. El bot confirma: «Tu Telegram se ha desvinculado de tu cuenta».
4. Dejará de reconocerte y cualquier mensaje posterior iniciará otra vez la vinculación.

## Notas

- No elimina tu cuenta ni conversaciones de Aurvek; el historial sigue en la web.
- No borra el chat de Telegram, aunque el bot deja de responder como usuario vinculado.
- Para volver a vincularlo, envía cualquier mensaje y comparte el teléfono de nuevo.
- Solo se puede desvincular desde el bot, no desde la web de Aurvek.
- Un administrador puede borrar `telegram_chat_id` en el panel para desvincular a un usuario.

## Relacionado

- telegram_setup
- telegram_commands
