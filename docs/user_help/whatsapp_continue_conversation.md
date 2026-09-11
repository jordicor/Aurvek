---
id: whatsapp_continue_conversation
title: Continue a web conversation on WhatsApp
category: whatsapp
keywords:
  - whatsapp
  - continue conversation
  - assign conversation
  - continuar conversacion
  - usar whatsapp
  - vincular whatsapp
  - external platform
  - messaging
prerequisites:
  - A verified phone number in your account settings
  - An existing conversation in the web chat
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
---

## Short answer

Assign a web conversation to WhatsApp from its sidebar menu with **Use for WhatsApp**. Messages sent to the Aurvek WhatsApp number then continue in that conversation.

## Steps

1. Open the chat sidebar and find the conversation you want to continue on WhatsApp.
2. Click the three-dot menu on the conversation.
3. Select **Use for WhatsApp**.
4. If you have not set a phone number yet, a prompt will ask you to add one in Settings first.
5. The conversation will move to the **External** section in the sidebar and display a WhatsApp icon.
6. Send a message from your phone to the Aurvek WhatsApp number. It will be delivered to that conversation.

To stop using a conversation on WhatsApp, open the same menu and select **Remove from WhatsApp**.

## Notes

- Only one conversation can be assigned to WhatsApp at a time. Assigning a new one automatically unlinks the previous one.
- A conversation cannot be on both WhatsApp and Telegram simultaneously. Assigning it to WhatsApp removes it from Telegram, and vice versa.
- Its telephone assignment is separate and can stay active.
- If you have never used WhatsApp before, the system creates a new conversation for you automatically on your first message. You can then reassign it to an existing conversation from the web.
- You can switch between **Text Mode** and **Voice Mode** from the conversation menu once WhatsApp is assigned. Voice mode sends audio responses instead of text.
- From WhatsApp itself, you can send `!chats` to list your recent conversations and `!set <id>` to switch without opening the web interface.
- Messages sent through WhatsApp appear in the same conversation history visible on the web.
- If the assigned conversation is locked, WhatsApp will reject new messages. Send `!new` to start a fresh one.

## Related

- whatsapp_commands
- whatsapp_setup_phone
- external_manage_conversations
- phone_calls_usage
