---
id: chat_export_mp3
title: "Exporter une conversation en audio (MP3)"
category: chat
keywords:
  - "MP3"
  - "audio"
  - "exporter"
  - "télécharger"
  - "synthèse vocale"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-04
locale: fr
base_source_hash: d28dd6f8f08df51ce23978820fe8844e2486a8bbdd846dea5f0f6aeef3480319
---

## Réponse courte

Exportez une conversation entière en MP3. L’audio original conservé d’un appel ou d’une note vocale WhatsApp/Telegram est réutilisé ; les autres messages sont synthétisés, puis tous les segments sont assemblés dans l’ordre.

## Étapes

1. Dans la barre latérale, ouvrez le menu à trois points de la conversation et choisissez **Télécharger le MP3**.
2. Confirmez. La génération démarre en arrière-plan.
3. Une fois terminée, ouvrez la **Galerie multimédia** pour trouver et télécharger le fichier.

## Remarques

- La durée dépend de la conversation. Un enregistrement individuel exceptionnellement long peut utiliser la TTS de secours.
- L’audio téléphonique et les notes vocales conservés gardent leurs voix ; les autres messages utilisent les voix configurées du prompt et du compte.
- Le nom comprend le prompt et un horodatage, par exemple `My_Prompt_2026_03_22_14_30_00.mp3`.
- Si une génération est déjà en cours, attendez quelques minutes avant d’en demander une autre pour la même conversation.
- Seules les parties nécessitant la TTS débitent le solde ; réutiliser l’audio original n’ajoute aucun coût TTS.

## Articles associés

- chat_export_pdf
- tts_listen_messages
