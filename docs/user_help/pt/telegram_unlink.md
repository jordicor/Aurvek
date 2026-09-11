---
id: telegram_unlink
title: "Como desassociar Telegram da sua conta Aurvek"
category: telegram
keywords:
  - "Telegram"
  - "desassociar"
  - "desligar"
  - "remover"
prerequisites:
  - "Uma conta Telegram atualmente associada ao Aurvek (consulte telegram_setup)"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: pt
base_source_hash: 856229a852405b67ff49f83d0bfc50d35d10122514120cb535d0884030d4a2a5
---

## Resposta breve

Envie `!unlink` ao bot Aurvek. A associação entre Telegram e Aurvek é removida imediatamente.

## Passos

1. Abra o bot, escreva `!unlink` e envie.
2. O bot confirma: «O seu Telegram foi desassociado da sua conta.» Mensagens seguintes voltam a pedir a associação.

## Notas

- Não elimina a conta Aurvek, conversas ou mensagens Telegram.
- O bot deixa de o reconhecer até voltar a associar partilhando o telefone.
- Só pode desassociar pelo bot, não pela web.
- Um administrador pode limpar `telegram_chat_id` no painel.

## Relacionado

- telegram_setup
- telegram_commands
