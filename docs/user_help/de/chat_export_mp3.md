---
id: chat_export_mp3
title: Eine Unterhaltung als Audio (MP3) exportieren
category: chat
keywords:
- MP3
- Audio
- Export
- Herunterladen
- Sprachausgabe
- TTS
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-13
locale: de
base_source_hash: b3c1ef1899fa03562f45b2774ce0b7d2121a2ed97f46e036e5ae1b9968a2ac6d
---

## Kurzantwort

Sie können einen ganzen Chat als MP3 exportieren. Für abgeschlossene Telefonate wird die gespeicherte durchgehende Aufnahme einmal verwendet; beide Stimmen, Pausen und gleichzeitiges Sprechen bleiben erhalten. Andere gespeicherte Audios, etwa WhatsApp-/Telegram-Sprachnotizen, bleiben in Gesprächsreihenfolge. Nur Nachrichten ohne verwendbares Audio benötigen Sprachsynthese (TTS).

## Schritte

1. In der Seitenleiste das Dreipunktmenü der Unterhaltung öffnen.
2. **MP3 herunterladen** wählen.
3. Im Dialog bestätigen.
4. Die MP3-Erstellung wird im Hintergrund eingereiht; eine Meldung bestätigt den Start.
5. Nach Fertigstellung die Datei in der **Mediengalerie** finden und herunterladen.

## Hinweise

- Die vollständige Aufnahme wird verwendet, wenn das Telefonat vollständig zu diesem Chat gehört und seine Nachrichten einen zusammenhängenden Block bilden. Andernfalls oder bei fehlender Aufnahme werden die verfügbaren Audios der einzelnen Nachrichten verwendet.
- Die Hintergrundverarbeitung kann je nach Gesprächslänge dauern.
- Aufbewahrtes Telefon- und Sprachnotizaudio behält die Originalstimmen. Bot- und Nutzernachrichten ohne Audio verwenden die konfigurierten Prompt- und Kontostimmen.
- Außergewöhnlich lange Einzelaufnahmen können für zuverlässige Exporte durch TTS ersetzt werden.
- Dateiname: Prompt-Name und Zeitstempel, z. B. `My_Prompt_2026_03_22_14_30_00.mp3`.
- Läuft bereits eine Erstellung, warten Sie einige Minuten vor einer weiteren Anfrage für denselben Chat.
- Nur Abschnitte mit TTS verbrauchen Guthaben. Wiederverwendetes Originalaudio verursacht keine zusätzliche TTS-Gebühr.

## Verwandte Artikel

- chat_export_pdf
- tts_listen_messages
