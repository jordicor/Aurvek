---
id: tts_listen_messages
title: "Écouter les messages de l’IA avec la synthèse vocale"
category: chat
keywords:
  - "TTS"
  - "synthèse vocale"
  - "écouter"
  - "audio"
  - "voix"
  - "lire un message"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: fr
base_source_hash: 0f5bb5a58de885d9c9b89a64102fbab81940cd0d83c3f0ed8279e5dd773e04c7
---

## Réponse courte

Cliquez sur le haut-parleur d’un message pour convertir son texte en parole diffusée en temps réel. Un audio déjà lu se charge immédiatement depuis le cache.

## Étapes

1. Survolez un message ou utilisez les actions d’un message bot, puis cliquez sur l’**icône haut-parleur**.
2. Un sablier s’affiche au chargement, puis une icône **arrêt** pendant la lecture ; cliquez pour arrêter.
3. Cliquez sur un autre haut-parleur pour arrêter automatiquement l’audio actuel et lire l’autre.

## Remarques

- Le cache est vérifié avant toute génération. Sinon, l’audio est généré et diffusé par WebSocket avant la fin du traitement.
- Chaque génération TTS débite le solde ; la relecture en cache est gratuite. Un solde insuffisant affiche une notification.
- Les messages utilisateur et bot sont lisibles ; la voix dépend de l’auteur et de la configuration vocale du prompt.

## Articles associés

- audio_input_stt
- chat_export_mp3
