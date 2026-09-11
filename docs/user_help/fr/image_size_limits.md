---
id: image_size_limits
title: "Pourquoi les images envoyées à l’IA sont redimensionnées à 1568 pixels"
category: limitations
keywords:
  - "taille d’image"
  - "qualité d’image"
  - "résolution d’image"
  - "image redimensionnée"
  - "image floue"
  - "qualité de capture d’écran"
  - "1568"
  - "limites d’image fournisseur"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-04-14
locale: fr
base_source_hash: 398c00ba92296bf81e63380a1cecb2b34709488fea96970c4ba964726d3e7eba
---

## Réponse courte

Avant l’envoi à l’IA, Aurvek réduit une image jointe afin que son côté le plus long mesure au plus 1568 pixels. Les proportions sont conservées sans recadrage. Cela concerne seulement les images des messages, pas les photos de profil, avatars de prompt ni fonds de thème.

## Étapes

1. Joignez l’image normalement ; le redimensionnement est automatique.
2. Pour une capture avec petit texte, recadrez la zone utile ; plusieurs captures ciblées sont souvent plus lisibles qu’une grande image.

## Remarques

- Aurvek stocke et affiche la version réduite (1568 px maximum). Les octets originaux en pleine résolution ne sont plus conservés après l’enregistrement.
- La limite vise les images téléversées, pas celles générées par DALL-E, Ideogram, Gemini, etc.

## Articles associés

- file_uploads
- image_generation
- limitations_file_types
