---
id: telegram_unlink
title: "Scollegare Telegram dall’account Aurvek"
category: telegram
keywords:
  - "Telegram"
  - "scollegare"
  - "disconnettere"
  - "rimuovere"
prerequisites:
  - "Un account Telegram attualmente collegato ad Aurvek (vedi telegram_setup)"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: it
base_source_hash: 856229a852405b67ff49f83d0bfc50d35d10122514120cb535d0884030d4a2a5
---

## Risposta breve

Invia `!unlink` al bot Aurvek in Telegram per rimuovere immediatamente il collegamento tra i due account.

## Passaggi

1. Apri la conversazione col bot e invia `!unlink`.
2. Il bot conferma: «Telegram è stato scollegato dal tuo account.»
3. Il bot non riconosce più l’account; ogni nuovo messaggio riavvia il collegamento.

## Note

- Non vengono eliminati account Aurvek, conversazioni o chat Telegram; il bot smette soltanto di rispondere come utente collegato.
- Per ricollegarti, invia un messaggio e condividi di nuovo il numero.
- L’utente può scollegarsi solo dal bot, non dall’interfaccia web.
- Un amministratore può cancellare il campo `telegram_chat_id` nel pannello amministrativo.

## Articoli correlati

- telegram_setup
- telegram_commands
