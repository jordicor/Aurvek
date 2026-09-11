---
id: audio_input_stt
title: Sprachnachrichten per Spracherkennung senden
category: chat
keywords:
- Sprache
- Mikrofon
- Sprache zu Text
- STT
- Audioeingabe
- Aufnahme
- erneut transkribieren
- Sprachnotiz
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: de
base_source_hash: e5f0b9351bfb8dc6d7d2c297e9ee4de62d51d62aee15a2132b13c582e426cc4c
---

## Kurzantwort

Nehmen Sie per Mikrofon eine Sprachnachricht auf. Sie wird automatisch transkribiert und als normale Chatnachricht gesendet. Starten Sie mit der Mikrofontaste und senden oder verwerfen Sie die Aufnahme.

## Schritte

1. **Mikrofonsymbol** neben dem Eingabefeld anklicken.
2. Auf Nachfrage den Mikrofonzugriff im Browser erlauben.
3. Das Eingabefeld zeigt Aufnahmemodus, Timer und Bedienelemente. Das Mikrofonsymbol wird zum Stoppsymbol.
4. Nachricht sprechen.
5. Mit **Senden** (Pfeil) Aufnahme stoppen und zur Transkription senden; mit **Abbrechen** (X) verwerfen.
6. Der Server transkribiert das Audio. Der Text wird ins Eingabefeld eingefügt und automatisch als Chatnachricht gesendet.

## Hinweise

- Der Browser muss Audioaufnahmen unterstützen (MediaRecorder API); die meisten modernen Browser tun dies.
- Wurde der Mikrofonzugriff früher abgelehnt, ändern Sie die Website-Berechtigungen im Browser.
- Aufnahmeformat: WebM mit Opus-Codec.
- Transkription verbraucht Guthaben; bei zu wenig Guthaben erscheint eine Meldung.
- Kurze oder stille Aufnahmen können leere Transkripte liefern (Antwort 204); dann wird nichts gesendet.
- WhatsApp-/Telegram-Sprachnotizen nutzen den Ablauf für externe Kanäle. Wurde das Originalaudio aufbewahrt, können Sie später an der Nachricht eine neue Transkription anfordern, prüfen und annehmen, bevor sie den sichtbaren Text ersetzt.

## Verwandte Artikel

- tts_listen_messages
- chat_search
- external_platforms_overview
