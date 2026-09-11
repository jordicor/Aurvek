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
last_reviewed: 2026-09-04
locale: de
base_source_hash: d28dd6f8f08df51ce23978820fe8844e2486a8bbdd846dea5f0f6aeef3480319
---

## Kurzantwort

Exportieren Sie einen ganzen Chat als MP3. Aufbewahrtes Originalaudio von Telefonbeiträgen oder WhatsApp-/Telegram-Sprachnotizen bleibt erhalten. Nachrichten ohne Originalaudio werden vorgelesen; alle Abschnitte werden in Gesprächsreihenfolge verbunden.

## Schritte

1. In der Seitenleiste das Dreipunktmenü der Unterhaltung öffnen.
2. **MP3 herunterladen** wählen.
3. Im Dialog bestätigen.
4. Die MP3-Erstellung wird im Hintergrund eingereiht; eine Meldung bestätigt den Start.
5. Nach Fertigstellung die Datei in der **Mediengalerie** finden und herunterladen.

## Hinweise

- Die Hintergrundverarbeitung kann je nach Gesprächslänge dauern.
- Aufbewahrtes Telefon- und Sprachnotizaudio behält die Originalstimmen. Bot- und Nutzernachrichten ohne Audio verwenden die konfigurierten Prompt- und Kontostimmen.
- Außergewöhnlich lange Einzelaufnahmen können für zuverlässige Exporte durch TTS ersetzt werden.
- Dateiname: Prompt-Name und Zeitstempel, z. B. `My_Prompt_2026_03_22_14_30_00.mp3`.
- Läuft bereits eine Erstellung, warten Sie einige Minuten vor einer weiteren Anfrage für denselben Chat.
- Nur Abschnitte mit TTS verbrauchen Guthaben. Wiederverwendetes Originalaudio verursacht keine zusätzliche TTS-Gebühr.

## Verwandte Artikel

- chat_export_pdf
- tts_listen_messages
