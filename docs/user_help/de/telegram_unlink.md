---
id: telegram_unlink
title: Telegram vom Aurvek-Konto trennen
category: telegram
keywords:
- Telegram
- Verknüpfung aufheben
- trennen
- entfernen
- entknüpfen
prerequisites:
- Ein aktuell mit Aurvek verknüpftes Telegram-Konto (siehe telegram_setup)
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: de
base_source_hash: 856229a852405b67ff49f83d0bfc50d35d10122514120cb535d0884030d4a2a5
---

## Kurzantwort

Senden Sie `!unlink` an den Aurvek-Bot in Telegram. Die Verknüpfung zwischen Telegram und Aurvek wird sofort entfernt.

## Schritte

1. Telegram-Chat mit dem Aurvek-Bot öffnen.
2. `!unlink` senden.
3. Der Bot bestätigt: „Ihr Telegram wurde von Ihrem Konto getrennt.“
4. Er erkennt das Konto nicht mehr; weitere Nachrichten fordern zur erneuten Verknüpfung auf.

## Hinweise

- Aurvek-Konto und Unterhaltungen bleiben erhalten; der Verlauf bleibt im Web zugänglich.
- Auch der Telegram-Chat und seine Nachrichten bleiben bestehen. Der Bot antwortet Ihnen jedoch nicht mehr als verknüpftem Nutzer.
- Zur erneuten Verknüpfung eine beliebige Nachricht senden und die Telefonnummer erneut teilen (siehe telegram_setup).
- Derzeit ist Entknüpfen nicht in Aurveks Weboberfläche möglich, sondern nur über den Bot.
- Administratoren können im Adminbereich das Feld `telegram_chat_id` des Nutzers leeren.

## Verwandte Artikel

- telegram_setup
- telegram_commands
