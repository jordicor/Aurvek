---
id: external_manage_conversations
title: "Gestire le conversazioni da WhatsApp o Telegram"
category: chat
keywords:
  - "elencare conversazioni"
  - "cambiare conversazione"
  - "comando chats"
  - "comando set"
  - "gestire chat"
  - "assegnare conversazione WhatsApp"
  - "assegnare conversazione Telegram"
prerequisites:
  - "Un account WhatsApp o Telegram collegato ad Aurvek"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: it
base_source_hash: 5b8b56741a26a97b05c721945c2e88b9d8ecb91eb691ee1bddc106b8d5e9da65
---

## Risposta breve

Da WhatsApp o Telegram, `!chats` elenca le conversazioni e `!set` cambia quella del canale di messaggistica. L’assegnazione telefonica è separata e non cambia.

## Passaggi

1. Invia `!chats`: ogni riga mostra ID, titolo, numero di messaggi, ultima attività e badge; `->` indica la conversazione attiva.
2. Invia `!set <id>`, per esempio `!set 1234`, per assegnarla alla piattaforma corrente.
3. Per l’altra piattaforma: `!set 1234 telegram` da WhatsApp o `!set 1234 whatsapp` da Telegram. Funzionano anche `tg` e `wa`.
4. Il messaggio successivo usa quella conversazione. La precedente non viene eliminata; perde solo il collegamento alla piattaforma.

## Note

- Una conversazione attiva per piattaforma; una chat può avere solo WhatsApp o Telegram, mentre il telefono indipendente può restare attivo.
- Se la sposti dalla piattaforma attuale, il prossimo messaggio lì creerà automaticamente una nuova conversazione.
- Le chat bloccate non si possono assegnare: usa `!new`.
- La piattaforma di destinazione deve essere collegata. Puoi assegnare anche dalla barra web.
- **+ > Chiamami** gestisce il canale telefonico.

## Articoli correlati

- whatsapp_commands
- telegram_commands
- whatsapp_continue_conversation
- external_platforms_overview
- phone_calls_usage
