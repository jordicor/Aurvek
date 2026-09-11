---
id: external_manage_conversations
title: "Gestão de conversas pelo WhatsApp ou Telegram"
category: chat
keywords:
  - "listar conversas"
  - "mudar conversa"
  - "comando chats"
  - "comando set"
  - "gerir conversas"
  - "atribuir conversa"
prerequisites:
  - "Uma conta WhatsApp ou Telegram associada ao Aurvek"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: pt
base_source_hash: 5b8b56741a26a97b05c721945c2e88b9d8ecb91eb691ee1bddc106b8d5e9da65
---

## Resposta breve

Liste e mude conversas de mensagens com `!chats` e `!set`. A atribuição telefónica é separada e estes comandos não a alteram.

## Passos

1. Envie `!chats` para ver ID, título, número de mensagens, última atividade e plataforma; `->` marca a ativa.
2. Envie `!set <id>` para atribuir à plataforma atual, por exemplo `!set 1234`.
3. Use `!set 1234 telegram` no WhatsApp ou `!set 1234 whatsapp` no Telegram para mover; `tg` e `wa` também funcionam.
4. A mensagem seguinte vai para a escolhida; a anterior não é eliminada, apenas perde a associação.

## Notas

- Só uma conversa por plataforma pode estar ativa.
- WhatsApp e Telegram são mutuamente exclusivos; o telefone independente pode continuar.
- Ao mover da plataforma atual, a próxima mensagem nela cria automaticamente uma conversa.
- Conversas bloqueadas não podem ser atribuídas; use `!new`.
- A plataforma de destino deve estar associada. Também pode atribuir pela web.
- Use **+ > Ligar-me** para telefone.

## Relacionado

- whatsapp_commands
- telegram_commands
- whatsapp_continue_conversation
- external_platforms_overview
- phone_calls_usage
