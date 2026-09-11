---
id: telegram_commands
title: Befehle des Telegram-Bots
category: telegram
keywords:
- Telegram
- Befehle
- Bot
- Hilfe
- Text
- Stimme
- Prompt
- neuer Chat
- Verknüpfung aufheben
- Chats auflisten
- Chat wechseln
prerequisites:
- Ein mit Aurvek verknüpftes Telegram-Konto (siehe telegram_setup)
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: de
base_source_hash: 90283b72cdd9c9a04f67c402858c03345ac26776501d8a38c9552a6734298ec1
---

## Kurzantwort

Der Aurvek-Telegram-Bot unterstützt Befehle mit `!`: Text-/Sprachantworten, Prompt-Wechsel, Chatliste und -wechsel, neue Chats und Entknüpfen. `!help` zeigt alle Befehle.

## Schritte

1. **!help**: Alle verfügbaren Befehle anzeigen.
2. **!text**: KI-Antworten als Text.
3. **!voice**: Antworten als TTS-Audio; bei Fehlern als Text.
4. **!prompt list**: Zugängliche Prompts (KI-Persönlichkeiten) mit ID/Name, bis zu 20.
5. **!prompt \<name or id\>**: Aktiven Prompt per ID oder Name wechseln; Teilnamen möglich.
6. **!new**: Neuen Chat starten. Der bisherige bleibt gespeichert und im Web zugänglich.
7. **!unlink**: Telegram entknüpfen. Der Bot erkennt Sie erst nach erneuter Verknüpfung wieder.
8. **!chats**: Bis zu 15 aktuelle Chats mit ID, Titel, Nachrichtenanzahl und letzter Aktivität. Aktiver Chat: `->`.
9. **!set \<id\> [platform]**: Chat wechseln. `!set 1234` weist Telegram zu, `!set 1234 whatsapp` WhatsApp. Aliase: `wa`, `tg`; optionales `#` vor der ID.

## Hinweise

- Groß-/Kleinschreibung ist egal (`!Help`, `!HELP`, `!help`).
- Auch normale Texte, Sprachnachrichten und Fotos sind möglich; Sprache wird automatisch transkribiert.
- Bei gesperrtem Chat mit `!new` neu beginnen.
- Lange Antworten werden für Telegrams Längenlimits automatisch geteilt.
- `!prompt` sucht zuerst exakte ID, dann exakten Namen, dann Teilnamen.

## Verwandte Artikel

- telegram_setup
- telegram_unlink
- external_manage_conversations
