---
id: external_manage_conversations
title: Unterhaltungen über WhatsApp oder Telegram verwalten
category: chat
keywords:
- Unterhaltungen auflisten
- Chat wechseln
- chats-Befehl
- set-Befehl
- Chats verwalten
- WhatsApp zuweisen
- Telegram zuweisen
prerequisites:
- Ein mit Aurvek verknüpftes WhatsApp- oder Telegram-Konto
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: de
base_source_hash: 5b8b56741a26a97b05c721945c2e88b9d8ecb91eb691ee1bddc106b8d5e9da65
---

## Kurzantwort

Mit `!chats` und `!set` listen und wechseln Sie Chats direkt in WhatsApp oder Telegram. Die separat verwaltete Telefonzuweisung bleibt unverändert.

## Schritte

1. `!chats` zeigt die neuesten Chats mit ID, Titel, Nachrichtenanzahl, letzter Aktivität und Plattformzuordnung. Der aktive Chat ist mit `->` markiert.
2. `!set <id>`, etwa `!set 1234`, weist den Chat der Plattform zu, von der Sie schreiben.
3. Aus WhatsApp verschiebt `!set 1234 telegram` den Chat zu Telegram; aus Telegram verschiebt `!set 1234 whatsapp` ihn zu WhatsApp. Kurzformen: `tg` und `wa`.
4. Ihre nächste Nachricht dort geht in den gewählten Chat. Der vorherige bleibt erhalten und verliert nur seine Plattformbindung.

## Hinweise

- Pro Plattform ist jeweils ein Chat aktiv.
- Ein Chat kann nur WhatsApp oder Telegram zugewiesen sein; seine unabhängige Telefonzuweisung kann bestehen bleiben.
- Beim Verschieben weg von der gerade genutzten Plattform warnt das System: Ihre nächste Nachricht dort startet automatisch einen neuen Chat.
- Gesperrte Chats sind nicht zuweisbar. Starten Sie mit `!new` einen neuen.
- Die Zielplattform muss mit Ihrem Konto verknüpft sein.
- Zuweisungen sind auch in der Web-Seitenleiste möglich.
- Den Telefonkanal verwalten Sie im Webchat unter **+ > Ruf mich an**.

## Verwandte Artikel

- whatsapp_commands
- telegram_commands
- whatsapp_continue_conversation
- external_platforms_overview
- phone_calls_usage
