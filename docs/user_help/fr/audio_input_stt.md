---
id: audio_input_stt
title: "Envoyer des messages vocaux avec la transcription automatique"
category: chat
keywords:
  - "voix"
  - "microphone"
  - "reconnaissance vocale"
  - "STT"
  - "entrée audio"
  - "enregistrer"
  - "dictée"
  - "retranscrire"
  - "note vocale"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: fr
base_source_hash: e5f0b9351bfb8dc6d7d2c297e9ee4de62d51d62aee15a2132b13c582e426cc4c
---

## Réponse courte

Enregistrez un message avec le microphone : Aurvek le transcrit en texte et l’envoie comme message de discussion. Cliquez sur le microphone, puis envoyez ou annulez.

## Étapes

1. Cliquez sur l’**icône microphone** près du champ de message et autorisez l’accès si le navigateur le demande.
2. Le champ passe en mode enregistrement avec minuteur ; parlez.
3. Cliquez sur **Envoyer** (flèche) pour arrêter et transcrire, ou sur **Annuler** (X) pour supprimer l’enregistrement.
4. Après transcription sur le serveur, le texte est placé dans le champ puis envoyé automatiquement.

## Remarques

- Le navigateur doit prendre en charge MediaRecorder et avoir l’autorisation d’utiliser le microphone.
- Le format est WebM avec codec Opus.
- La transcription utilise le solde du compte. Un solde insuffisant produit une notification.
- Un enregistrement bref ou silencieux peut donner une transcription vide (réponse 204) ; aucun message n’est alors envoyé.
- Les notes vocales WhatsApp et Telegram suivent le flux des canaux externes. Si l’audio original est conservé, vous pouvez demander une nouvelle transcription, la vérifier et l’accepter avant qu’elle remplace le texte visible.

## Articles associés

- tts_listen_messages
- chat_search
- external_platforms_overview
