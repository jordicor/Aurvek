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
last_reviewed: 2026-09-13
locale: fr
base_source_hash: b3c1ef1899fa03562f45b2774ce0b7d2121a2ed97f46e036e5ae1b9968a2ac6d
---

## Réponse courte

Exportez une conversation entière en MP3. Chaque appel terminé utilise une seule fois son enregistrement continu conservé, avec les deux voix, les pauses et les paroles simultanées. Les autres audios conservés, dont les notes vocales WhatsApp/Telegram, restent dans l’ordre de la conversation. Seuls les messages sans audio utilisable nécessitent la synthèse vocale (TTS).

## Étapes

1. Dans la barre latérale, ouvrez le menu à trois points de la conversation et choisissez **Télécharger le MP3**.
2. Confirmez. La génération démarre en arrière-plan.
3. Une fois terminée, ouvrez la **Galerie multimédia** pour trouver et télécharger le fichier.

## Remarques

- L’enregistrement complet est utilisé si l’appel appartient entièrement à cette conversation et si ses messages forment un bloc consécutif. Sinon, ou si cet enregistrement manque, l’export utilise les audios individuels disponibles des messages.
- La durée dépend de la conversation. Un enregistrement individuel exceptionnellement long peut utiliser la TTS de secours.
- L’audio téléphonique et les notes vocales conservés gardent leurs voix ; les autres messages utilisent les voix configurées du prompt et du compte.
- Le nom comprend le prompt et un horodatage, par exemple `My_Prompt_2026_03_22_14_30_00.mp3`.
- Si une génération est déjà en cours, attendez quelques minutes avant d’en demander une autre pour la même conversation.
- Seules les parties nécessitant la TTS débitent le solde ; réutiliser l’audio original n’ajoute aucun coût TTS.

## Articles associés

- chat_export_pdf
- tts_listen_messages
