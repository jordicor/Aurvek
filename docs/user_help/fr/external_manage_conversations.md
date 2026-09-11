---
id: external_manage_conversations
title: "Gérer les conversations depuis WhatsApp ou Telegram"
category: chat
keywords:
  - "lister les conversations"
  - "changer de conversation"
  - "commande chats"
  - "commande set"
  - "gérer les discussions"
  - "affecter une conversation WhatsApp"
  - "affecter une conversation Telegram"
prerequisites:
  - "Un compte WhatsApp ou Telegram lié à Aurvek"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: fr
base_source_hash: 5b8b56741a26a97b05c721945c2e88b9d8ecb91eb691ee1bddc106b8d5e9da65
---

## Réponse courte

Depuis WhatsApp ou Telegram, `!chats` liste les conversations et `!set` change celle du canal de messagerie. L’affectation téléphonique est indépendante et reste inchangée.

## Étapes

1. Envoyez `!chats` : chaque ligne affiche ID, titre, nombre de messages, dernière activité et badges de plateforme ; `->` marque la conversation active.
2. Envoyez `!set <id>` (par exemple `!set 1234`) pour l’affecter à la plateforme actuelle.
3. Pour l’autre plateforme : `!set 1234 telegram` depuis WhatsApp ou `!set 1234 whatsapp` depuis Telegram. Les alias `tg` et `wa` fonctionnent.
4. Le message suivant utilise cette conversation. L’ancienne n’est pas supprimée, seul son lien de plateforme est retiré.

## Remarques

- Une conversation active par plateforme ; une conversation ne peut avoir qu’une affectation de messagerie, WhatsApp ou Telegram, tandis que le téléphone peut rester actif.
- Si vous la déplacez hors de la plateforme actuelle, le prochain message y créera automatiquement une nouvelle conversation.
- Une conversation verrouillée ne peut être affectée ; utilisez `!new`.
- La plateforme cible doit être liée. L’affectation est aussi possible depuis la barre latérale web.
- **+ > Appelez-moi** gère le canal téléphonique.

## Articles associés

- whatsapp_commands
- telegram_commands
- whatsapp_continue_conversation
- external_platforms_overview
- phone_calls_usage
