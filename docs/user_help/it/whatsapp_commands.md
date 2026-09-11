---
id: whatsapp_commands
title: "Comandi WhatsApp disponibili"
category: whatsapp
keywords:
  - "comandi WhatsApp"
  - "aiuto WhatsApp"
  - "modalità testo"
  - "modalità voce"
  - "cambiare prompt"
  - "nuova conversazione"
  - "elenco prompt"
  - "elencare conversazioni"
  - "cambiare conversazione"
prerequisites:
  - "Una conversazione WhatsApp attiva assegnata al tuo account"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: it
base_source_hash: a88730b3fdf5473fa22b95fecca28ab8a93e10bf1b964484b89d9d9bd31db2df
---

## Risposta breve

I comandi WhatsApp controllano la conversazione senza uscire dalla chat. Iniziano con `!` e ignorano maiuscole/minuscole.

## Passaggi

1. `!help` elenca i comandi. `!text` attiva risposte scritte (`text mode`, `text_mode`); `!voice` l’audio (`voice mode`, `voice_mode`).
2. `!prompt list` mostra fino a 20 prompt con ID e nome. `!prompt <name or id>` cambia per ID numerico, nome esatto, quindi parziale, per esempio `!prompt 42` o `!prompt email campaigns`.
3. `!new` crea una conversazione e conserva la precedente sul web.
4. `!chats` elenca ID, titolo, messaggi, data e badge; `->` indica l’attiva.
5. `!set <id> [platform]` cambia assegnazione: `!set 1234`, `!set 1234 telegram`, alias `wa`/`tg`, ID con o senza `#`.

## Note

- I comandi vengono elaborati prima dell’IA e non le vengono mai inviati.
- Nelle conversazioni bloccate i messaggi normali sono rifiutati; usa `!new`.
- La modalità voce usa TTS e può consumare saldo secondo il piano.
- Il comando `!prompt` prova l’ID esatto, poi il nome esatto e infine un nome parziale.

## Articoli correlati

- whatsapp_continue_conversation
- whatsapp_setup_phone
- external_manage_conversations
