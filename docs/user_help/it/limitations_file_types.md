---
id: limitations_file_types
title: "Tipi di file supportati e limiti di caricamento"
category: limitations
keywords:
  - "file"
  - "caricare"
  - "immagine"
  - "PDF"
  - "tipo di file"
  - "limite dimensione"
  - "formati supportati"
  - "allegato"
  - "file di testo"
  - "file di codice"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: it
base_source_hash: 6b207a41121e1dbaf8d1aae712481d6f0c45c0f94500a2340aed5d426f15012d
---

## Risposta breve

Aurvek accetta come allegati immagini, PDF e file di testo semplice o codice supportati. Rifiuta documenti Word, fogli di calcolo, audio, video e archivi. Il caricamento deve essere consentito per account e conversazione.

## Passaggi

1. Usa **+ > Allega file** o incolla un’immagine dagli appunti.
2. Rispetta i limiti per tipo e verifica che il modello supporti l’allegato.

## Note

- Sono accettati immagini comuni, PDF, TXT, Markdown, CSV, JSON, XML, HTML, Python, JavaScript/TypeScript, CSS, SQL, YAML, configurazioni, log, script shell ed estensioni di codice comuni.
- Immagini: massimo 10 per messaggio, meno di 20 MB ciascuna prima dell’elaborazione e 50 megapixel. Aurvek prova a ridurle al limite del provider, altrimenti le rifiuta.
- PDF: massimo 3, meno di 25 MB ciascuno e 1.000 pagine elaborate in totale per messaggio.
- Testo/codice: massimo 3 file, meno di 2 MB ciascuno. Limite combinato: 16 allegati.
- Gli appunti accettano solo immagini. xAI (Grok) converte WebP in JPEG; i PDF con GPT/xAI passano automaticamente da OpenRouter.
- Il caricamento è un permesso dell’account. Non è disponibile in Multi-IA o GranSabio.

## Articoli correlati

- limitations_unsupported_features
- limitations_free_models
- file_uploads
