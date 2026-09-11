---
id: whatsapp_commands
title: Verfügbare WhatsApp-Befehle
category: whatsapp
keywords:
- WhatsApp-Befehle
- WhatsApp-Hilfe
- Textmodus
- Sprachmodus
- Prompt wechseln
- neuer Chat
- Promptliste
- Chats auflisten
- Chat wechseln
prerequisites:
- Eine aktive, Ihrem Konto zugewiesene WhatsApp-Unterhaltung
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: de
base_source_hash: a88730b3fdf5473fa22b95fecca28ab8a93e10bf1b964484b89d9d9bd31db2df
---

## Kurzantwort

WhatsApp-Befehle steuern die Unterhaltung direkt im Chat. Alle beginnen mit `!`; Groß-/Kleinschreibung spielt keine Rolle.

## Schritte

1. **!help**: Verfügbare Befehle in WhatsApp anzeigen.
2. **!text**: Schriftliche KI-Antworten. Auch `text mode` oder `text_mode`.
3. **!voice**: Audioantworten. Auch `voice mode` oder `voice_mode`.
4. **!prompt list**: Bis zu 20 zugängliche Prompts mit ID und Name.
5. **!prompt \<name or id\>**: Prompt des aktiven Chats wechseln, per numerischer ID oder Name (auch teilweise). Beispiele: `!prompt 42`, `!prompt email campaigns`.
6. **!new**: Neuen Chat starten. Der vorherige bleibt gespeichert und im Web zugänglich.
7. **!chats**: Letzte Chats mit ID, Titel, Nachrichtenanzahl, letzter Aktivität und Plattformabzeichen. Aktiver WhatsApp-Chat: `->`.
8. **!set \<id\> [platform]**: `!set 1234` weist Chat #1234 WhatsApp zu; `!set 1234 telegram` verschiebt ihn zu Telegram. Aliase: `wa`, `tg`. ID mit oder ohne `#` möglich.

## Hinweise

- Befehle werden vor der KI verarbeitet; sie erhält sie nie als Eingabe.
- `!prompt` sucht exakte ID, dann exakten Namen ohne Beachtung der Großschreibung, dann Teilnamen.
- Gesperrte Chats blockieren normale Nachrichten; mit `!new` neu beginnen.
- Sprachmodus erzeugt TTS-Audio und kann je nach Tarif zusätzliches Guthaben verbrauchen.

## Verwandte Artikel

- whatsapp_continue_conversation
- whatsapp_setup_phone
- external_manage_conversations
