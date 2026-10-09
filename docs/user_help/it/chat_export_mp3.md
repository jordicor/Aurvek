---
id: chat_export_mp3
title: "Esportare una conversazione in audio (MP3)"
category: chat
keywords:
  - "MP3"
  - "audio"
  - "esportare"
  - "scaricare"
  - "sintesi vocale"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-13
locale: it
base_source_hash: b3c1ef1899fa03562f45b2774ce0b7d2121a2ed97f46e036e5ae1b9968a2ac6d
---

## Risposta breve

Esporta un’intera conversazione in MP3. Ogni chiamata terminata usa una sola volta la propria registrazione continua conservata, mantenendo entrambe le voci, le pause e il parlato simultaneo. Gli altri audio conservati, comprese le note vocali WhatsApp/Telegram, restano nell’ordine della conversazione. Solo i messaggi senza audio utilizzabile richiedono la sintesi vocale (TTS).

## Passaggi

1. Nel menu a tre punti della conversazione scegli **Scarica MP3**.
2. Conferma; la generazione parte in background.
3. Al termine, apri la **Galleria multimediale** per trovare e scaricare il file.

## Note

- La registrazione completa viene usata quando la chiamata appartiene interamente a questa conversazione e i suoi messaggi formano un blocco consecutivo. Negli altri casi, o se la registrazione manca, vengono usati gli audio individuali disponibili dei messaggi.
- Il tempo dipende dalla lunghezza. Una singola registrazione eccezionalmente lunga può usare la TTS di ripiego.
- Audio telefonico e note vocali conservati mantengono le voci originali; gli altri messaggi usano le voci configurate del prompt e dell’account.
- Il nome contiene prompt e data/ora, per esempio `My_Prompt_2026_03_22_14_30_00.mp3`.
- Se è già in corso una generazione, attendi alcuni minuti prima di ripeterla per la stessa conversazione.
- Solo le parti TTS consumano saldo; riutilizzare l’audio originale non aggiunge costi TTS.

## Articoli correlati

- chat_export_pdf
- tts_listen_messages
