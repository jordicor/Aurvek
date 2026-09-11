---
id: tts_listen_messages
title: "Ascoltare i messaggi IA con sintesi vocale"
category: chat
keywords:
  - "TTS"
  - "sintesi vocale"
  - "ascoltare"
  - "audio"
  - "voce"
  - "riprodurre messaggio"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: it
base_source_hash: 0f5bb5a58de885d9c9b89a64102fbab81940cd0d83c3f0ed8279e5dd773e04c7
---

## Risposta breve

Premi l’altoparlante di un messaggio per convertire il testo in voce trasmessa in tempo reale. Un audio già riprodotto si carica subito dalla cache.

## Passaggi

1. Passa su un messaggio o usa le azioni di un messaggio bot, quindi premi l’**icona altoparlante**.
2. Durante il caricamento appare una clessidra, poi un’icona **stop**; premila per fermare.
3. Premi l’altoparlante di un altro messaggio per interrompere automaticamente l’audio attuale e riprodurre l’altro.

## Note

- Il sistema controlla prima la cache. Se assente, genera e trasmette via WebSocket, avviando la riproduzione prima della fine dell’elaborazione.
- Ogni generazione TTS usa il saldo; la riproduzione dalla cache è gratuita. Un saldo insufficiente mostra una notifica.
- Si possono ascoltare messaggi utente e bot; la voce dipende dall’autore e dalla configurazione del prompt.

## Articoli correlati

- audio_input_stt
- chat_export_mp3
