---
id: whatsapp_commands
title: "Comandos disponíveis no WhatsApp"
category: whatsapp
keywords:
  - "comandos WhatsApp"
  - "ajuda WhatsApp"
  - "modo texto"
  - "modo voz"
  - "mudar prompt"
  - "nova conversa"
  - "listar conversas"
  - "mudar conversa"
prerequisites:
  - "Uma conversa WhatsApp ativa atribuída à sua conta"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: pt
base_source_hash: a88730b3fdf5473fa22b95fecca28ab8a93e10bf1b964484b89d9d9bd31db2df
---

## Resposta breve

WhatsApp aceita comandos iniciados por `!`, sem distinguir maiúsculas, para controlar a conversa.

## Passos

1. `!help`: lista; `!text`: texto, também `text mode`/`text_mode`; `!voice`: áudio, também `voice mode`/`voice_mode`.
2. `!prompt list`: até 20 prompts; `!prompt <name or id>` muda por ID ou nome parcial, por exemplo `!prompt 42` ou `!prompt email campaigns`.
3. `!new`: nova conversa, guardando a anterior.
4. `!chats`: lista conversas; `->` marca a ativa.
5. `!set <id> [platform]`: `!set 1234` atribui a WhatsApp; `!set 1234 telegram` move para Telegram. Aceita `wa`, `tg` e `#` opcional.

## Notas

- Comandos são processados antes da IA.
- `!prompt` tenta ID, nome exato e nome parcial.
- Em conversa bloqueada, use `!new`.
- Voz usa TTS e pode consumir saldo adicional.

## Relacionado

- whatsapp_continue_conversation
- whatsapp_setup_phone
- external_manage_conversations
