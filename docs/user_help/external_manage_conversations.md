---
id: external_manage_conversations
title: Manage conversations from WhatsApp or Telegram
category: chat
keywords:
  - list conversations
  - switch conversation
  - change conversation
  - chats command
  - set command
  - manage chats
  - listar conversaciones
  - cambiar conversacion
  - gestionar chats
  - ver conversaciones
  - asignar conversacion
  - assign conversation whatsapp
  - assign conversation telegram
prerequisites:
  - A WhatsApp or Telegram account linked to Aurvek
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
---

## Short answer

You can list and switch messaging conversations directly from WhatsApp or Telegram using `!chats` and `!set`. Telephone assignment is managed separately and is not changed by these commands.

## Steps

1. **See your recent conversations:** Send `!chats` to get a list of your most recent conversations. Each line shows the conversation ID, title, message count, last activity date, and which platform it is assigned to. Your current active conversation is marked with `->`.
2. **Switch to an existing conversation:** Send `!set <id>` to assign that conversation to the platform you are messaging from. Example: `!set 1234`.
3. **Move a conversation to the other platform:** Send `!set 1234 telegram` from WhatsApp to move that conversation to Telegram, or `!set 1234 whatsapp` from Telegram to move it to WhatsApp. Short aliases also work: `tg` and `wa`.
4. **Continue chatting:** Your next message on that platform will go to the selected conversation. The previous conversation is not deleted; it only loses the platform binding.

## Notes

- Only one conversation can be active per platform at a time.
- A conversation can only use one messaging assignment at a time: WhatsApp or Telegram. Its independent telephone assignment may remain active.
- If you move a conversation away from the platform you are currently using, the system warns you that your next message there will start a new conversation automatically.
- Locked conversations cannot be assigned. Use `!new` to start a fresh one instead.
- Cross-platform assignment requires the target platform to be linked to your account.
- You can also assign conversations from the web sidebar.
- Use **+ > Call me** in the web chat to assign or manage the telephone channel.

## Related

- whatsapp_commands
- telegram_commands
- whatsapp_continue_conversation
- external_platforms_overview
- phone_calls_usage
