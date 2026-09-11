---
id: telegram_commands
title: "Comandi del bot Telegram"
category: telegram
keywords:
  - "Telegram"
  - "comandi"
  - "bot"
  - "aiuto"
  - "testo"
  - "voce"
  - "prompt"
  - "nuova conversazione"
  - "scollegare"
  - "elencare conversazioni"
  - "cambiare conversazione"
prerequisites:
  - "Un account Telegram collegato ad Aurvek (vedi telegram_setup)"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: it
base_source_hash: 90283b72cdd9c9a04f67c402858c03345ac26776501d8a38c9552a6734298ec1
---

## Risposta breve

Il bot Telegram di Aurvek accetta comandi con prefisso `!` per testo o voce, prompt, conversazioni e scollegamento. Scrivi `!help` per l’elenco completo.

## Passaggi

1. `!help` mostra i comandi; `!text` attiva il testo; `!voice` la TTS con ripiego sul testo se la generazione fallisce.
2. `!prompt list` elenca fino a 20 prompt con ID e nome; `!prompt <name or id>` sceglie per ID, nome esatto, quindi nome parziale.
3. `!new` crea una conversazione e conserva la precedente sul web; `!unlink` scollega Telegram.
4. `!chats` elenca fino a 15 conversazioni con ID, titolo, messaggi e ultima attività; `->` indica quella attiva.
5. `!set <id> [platform]` cambia assegnazione: `!set 1234`, `!set 1234 whatsapp`, alias `wa`/`tg`, con `#` facoltativo.

## Note

- Non distingue maiuscole e minuscole: `!Help`, `!HELP` e `!help` sono equivalenti.
- Puoi inviare testo, messaggi vocali o foto; la voce viene trascritta automaticamente.
- Se la conversazione è bloccata, usa `!new`.
- Le risposte lunghe vengono divise secondo i limiti di Telegram.
- Il comando `!prompt` prova l’ID esatto, poi il nome esatto e infine un nome parziale.

## Articoli correlati

- telegram_setup
- telegram_unlink
- external_manage_conversations
