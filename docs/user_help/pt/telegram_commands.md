---
id: telegram_commands
title: "Comandos do bot Telegram"
category: telegram
keywords:
  - "Telegram"
  - "comandos"
  - "bot"
  - "ajuda"
  - "texto"
  - "voz"
  - "prompt"
  - "nova conversa"
  - "desassociar"
  - "listar"
  - "mudar conversa"
prerequisites:
  - "Uma conta Telegram associada ao Aurvek (consulte telegram_setup)"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: pt
base_source_hash: 90283b72cdd9c9a04f67c402858c03345ac26776501d8a38c9552a6734298ec1
---

## Resposta breve

O bot aceita comandos com `!` para texto/voz, prompt, conversas e desassociação. Escreva `!help` para a lista.

## Passos

1. `!help`: comandos; `!text`: texto; `!voice`: áudio TTS com recurso a texto.
2. `!prompt list`: até 20 prompts com ID/nome; `!prompt <name or id>`: muda por ID ou nome parcial.
3. `!new`: nova conversa, guardando a anterior; `!unlink`: desassocia.
4. `!chats`: até 15 conversas; `->` marca a ativa.
5. `!set <id> [platform]`: muda a ativa. `!set 1234` atribui a Telegram; `!set 1234 whatsapp`, a WhatsApp. Aceita `wa`, `tg` e `#` opcional.

## Notas

- Não distingue maiúsculas: `!Help`, `!HELP` e `!help` funcionam.
- Aceita texto, voz e fotografias; voz é transcrita.
- Em conversa bloqueada, use `!new`.
- Respostas longas são divididas.
- `!prompt` tenta ID exato, nome exato e nome parcial.

## Relacionado

- telegram_setup
- telegram_unlink
- external_manage_conversations
