---
id: whatsapp_continue_conversation
title: "Continuer une conversation web sur WhatsApp"
category: whatsapp
keywords:
  - "WhatsApp"
  - "continuer une conversation"
  - "affecter une conversation"
  - "utiliser WhatsApp"
  - "lier WhatsApp"
  - "canal externe"
  - "messagerie"
prerequisites:
  - "Un numéro de téléphone vérifié dans les paramètres du compte"
  - "Une conversation existante dans le chat web"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: fr
base_source_hash: 34476ae7269e19d5479fa0e2354962262bd9fdc0f75d755c4dadf6ed72bf8413
---

## Réponse courte

Dans le menu latéral d’une conversation, choisissez **Utiliser pour WhatsApp**. Les messages envoyés au numéro WhatsApp Aurvek continuent alors cette conversation.

## Étapes

1. Ouvrez le menu à trois points de la conversation et choisissez **Utiliser pour WhatsApp**.
2. Sans numéro enregistré, ajoutez-le d’abord dans Paramètres.
3. La conversation passe sous **Externe** avec l’icône WhatsApp.
4. Envoyez un message depuis votre téléphone au numéro WhatsApp Aurvek.
5. Pour arrêter, choisissez **Retirer de WhatsApp** dans le même menu.

## Remarques

- Une seule conversation WhatsApp. En affecter une autre dissocie la précédente.
- Une conversation ne peut être sur WhatsApp et Telegram à la fois ; le téléphone reste indépendant.
- À la première utilisation, Aurvek peut créer automatiquement une conversation, réaffectable depuis le web.
- Le menu permet **Mode texte** ou **Mode voix** ; la voix renvoie de l’audio.
- Dans WhatsApp, `!chats` liste et `!set <id>` change la conversation.
- Les messages restent visibles sur le web. Si la conversation est verrouillée, WhatsApp refuse les messages ; utilisez `!new`.

## Articles associés

- whatsapp_commands
- whatsapp_setup_phone
- external_manage_conversations
- phone_calls_usage
