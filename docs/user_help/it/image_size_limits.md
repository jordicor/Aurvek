---
id: image_size_limits
title: "Perché le immagini inviate all’IA vengono ridimensionate a 1568 pixel"
category: limitations
keywords:
  - "dimensione immagine"
  - "qualità immagine"
  - "risoluzione immagine"
  - "immagine ridimensionata"
  - "immagine sfocata"
  - "qualità screenshot"
  - "1568"
  - "limiti immagini provider"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-04-14
locale: it
base_source_hash: 398c00ba92296bf81e63380a1cecb2b34709488fea96970c4ba964726d3e7eba
---

## Risposta breve

Prima dell’invio all’IA, Aurvek ridimensiona un’immagine allegata affinché il lato lungo misuri al massimo 1568 pixel. Mantiene le proporzioni senza ritagli. Il limite riguarda solo le immagini nei messaggi, non foto profilo, avatar dei prompt o sfondi dei temi.

## Passaggi

1. Allega normalmente l’immagine; il ridimensionamento è automatico.
2. Per screenshot con testo piccolo, ritaglia la zona utile: più catture mirate sono spesso migliori di una grande.

## Note

- Aurvek conserva e mostra la versione ridotta (massimo 1568 px). Dopo il salvataggio non mantiene i byte originali a piena risoluzione.
- Il limite vale per le immagini caricate, non per quelle generate da DALL-E, Ideogram, Gemini, ecc.

## Articoli correlati

- file_uploads
- image_generation
- limitations_file_types
