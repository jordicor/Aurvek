---
id: tts_listen_messages
title: KI-Nachrichten mit Sprachausgabe anhören
category: chat
keywords:
- TTS
- Text zu Sprache
- anhören
- Audio
- Stimme
- Nachricht abspielen
- Sprachausgabe
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: de
base_source_hash: 0f5bb5a58de885d9c9b89a64102fbab81940cd0d83c3f0ed8279e5dd773e04c7
---

## Kurzantwort

Mit dem Lautsprechersymbol hören Sie jede Chatnachricht an. Der Text wird in Sprache umgewandelt und in Echtzeit zum Browser gestreamt. Bereits abgespieltes Audio lädt sofort aus dem Cache.

## Schritte

1. Maus über eine Nachricht bewegen oder Bot-Aktionssymbole ansehen; **Lautsprechersymbol** anklicken.
2. Beim Laden erscheint eine Sanduhr.
3. Während der Wiedergabe erscheint **Stopp**; damit jederzeit anhalten.
4. Der Lautsprecher einer anderen Nachricht startet diese und stoppt laufendes Audio automatisch.

## Hinweise

- Zuerst wird der Cache geprüft. Vorhandenes Audio startet sofort ohne Neuerzeugung.
- Sonst wird Audio erzeugt und per WebSocket in Echtzeit gestreamt; die Wiedergabe beginnt vor Verarbeitung der gesamten Nachricht.
- Jede TTS-Erzeugung kostet Guthaben; Wiedergabe aus dem Cache ist kostenlos.
- Bei zu wenig Guthaben erscheint statt Audio eine Meldung.
- Nutzer- und Botnachrichten sind abspielbar. Die Stimme hängt vom Autor und der Prompt-Stimmenkonfiguration ab.

## Verwandte Artikel

- audio_input_stt
- chat_export_mp3
