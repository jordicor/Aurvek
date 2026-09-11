---
id: limitations_file_types
title: "Types de fichiers pris en charge et limites de téléversement"
category: limitations
keywords:
  - "fichier"
  - "téléverser"
  - "image"
  - "PDF"
  - "type de fichier"
  - "taille maximale"
  - "formats pris en charge"
  - "pièce jointe"
  - "fichier texte"
  - "fichier de code"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: fr
base_source_hash: 6b207a41121e1dbaf8d1aae712481d6f0c45c0f94500a2340aed5d426f15012d
---

## Réponse courte

Aurvek accepte en pièce jointe les images, PDF et fichiers texte brut ou code pris en charge. Les documents Word, feuilles de calcul bureautiques, fichiers audio, vidéo et archives sont refusés. Le compte et la conversation doivent autoriser les téléversements.

## Étapes

1. Utilisez **+ > Joindre des fichiers** ou collez une image du presse-papiers.
2. Respectez les limites par type et vérifiez que le modèle accepte la pièce jointe.

## Remarques

- Types acceptés : images courantes, PDF, TXT, Markdown, CSV, JSON, XML, HTML, Python, JavaScript/TypeScript, CSS, SQL, YAML, configurations, journaux, scripts shell et extensions de code courantes.
- Images : 10 par message, moins de 20 MB chacune avant traitement, 50 mégapixels maximum. Aurvek tente de les réduire à la limite du fournisseur, sinon les refuse.
- PDF : 3 par message, moins de 25 MB chacun et 1 000 pages traitées au total par message.
- Texte/code : 3 fichiers par message, moins de 2 MB chacun. Maximum combiné : 16 pièces jointes.
- Le presse-papiers accepte uniquement les images. xAI (Grok) convertit WebP en JPEG ; les PDF avec GPT/xAI passent automatiquement par OpenRouter.
- Les téléversements sont une permission du compte. Ils sont indisponibles en Multi-IA et GranSabio.

## Articles associés

- limitations_unsupported_features
- limitations_free_models
- file_uploads
