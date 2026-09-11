---
id: audio_input_stt
title: "Inviare messaggi vocali con conversione da voce a testo"
category: chat
keywords:
  - "voce"
  - "microfono"
  - "conversione voce testo"
  - "STT"
  - "input audio"
  - "registrare"
  - "dettatura"
  - "ritrascrivere"
  - "nota vocale"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: it
base_source_hash: e5f0b9351bfb8dc6d7d2c297e9ee4de62d51d62aee15a2132b13c582e426cc4c
---

## Risposta breve

Registra un messaggio con il microfono: Aurvek lo trascrive e lo invia come normale messaggio di chat. Premi il microfono, quindi invia o annulla.

## Passaggi

1. Premi l’**icona del microfono** accanto al campo e autorizza l’accesso se richiesto.
2. Il campo passa alla registrazione con timer; parla.
3. Premi **Invia** (freccia) per fermare e trascrivere, oppure **Annulla** (X) per scartare.
4. Dopo la trascrizione sul server, il testo viene inserito nel campo e inviato automaticamente.

## Note

- Il browser deve supportare MediaRecorder e avere il permesso per il microfono.
- Il formato è WebM con codec Opus.
- La trascrizione usa il saldo dell’account; se insufficiente appare una notifica.
- Una registrazione breve o silenziosa può produrre una trascrizione vuota (risposta 204), senza inviare messaggi.
- Le note vocali WhatsApp e Telegram seguono il flusso dei canali esterni. Se l’audio originale è conservato, puoi richiedere una nuova trascrizione, rivederla e accettarla prima che sostituisca il testo visibile.

## Articoli correlati

- tts_listen_messages
- chat_search
- external_platforms_overview
