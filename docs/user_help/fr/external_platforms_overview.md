---
id: external_platforms_overview
title: "Utiliser les canaux externes avec Aurvek"
category: chat
keywords:
  - "canaux externes"
  - "WhatsApp"
  - "Telegram"
  - "messagerie"
  - "appels téléphoniques"
  - "retranscrire une note vocale"
  - "affecter une conversation"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: fr
base_source_hash: fbdebde8c95502f19d23439c0ba69e658c5fae393958be28934480a1d778157e
---

## Réponse courte

Continuez les conversations par WhatsApp, Telegram et téléphone. WhatsApp et Telegram sont deux affectations alternatives : une conversation ne peut en utiliser qu’une à la fois. Le téléphone est indépendant et peut coexister avec l’une d’elles.

## Étapes

1. Dans la barre latérale, ouvrez le menu à trois points d’une conversation.
2. Choisissez **Utiliser pour WhatsApp**, affectez-la depuis le bot Telegram, ou ouvrez **+ > Appelez-moi** pour le téléphone.
3. Les messages et transcriptions d’appels rejoignent la conversation et restent synchronisés avec le web.
4. Pour arrêter un canal, ouvrez ses commandes et retirez l’affectation.

## Remarques

- Une seule messagerie par conversation et une conversation active par canal. Une conversation compatible avec plusieurs canaux apparaît une fois sous **Externe**, avec plusieurs badges.
- WhatsApp exige un numéro vérifié ; Telegram doit d’abord être lié au bot Aurvek.
- Les deux acceptent texte, images et voix, mais pas les documents Word, feuilles de calcul, etc.
- Si l’audio original d’une note est conservé, le message web peut le lire et proposer **Retranscrire la note vocale originale**. Le nouveau texte ne remplace l’ancien qu’après vérification et acceptation ; cela peut consommer le solde ou des crédits fournisseur.
- `!help`, `!new`, `!text`, `!voice`, `!prompt`, `!chats` et `!set` fonctionnent sur les deux plateformes.

## Articles associés

- whatsapp_continue_conversation
- whatsapp_commands
- telegram_setup
- telegram_commands
- external_manage_conversations
- phone_calls_usage
