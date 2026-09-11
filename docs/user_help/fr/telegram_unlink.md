---
id: telegram_unlink
title: "Dissocier Telegram de votre compte Aurvek"
category: telegram
keywords:
  - "Telegram"
  - "dissocier"
  - "déconnecter"
  - "retirer"
prerequisites:
  - "Un compte Telegram actuellement lié à Aurvek (voir telegram_setup)"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: fr
base_source_hash: 856229a852405b67ff49f83d0bfc50d35d10122514120cb535d0884030d4a2a5
---

## Réponse courte

Envoyez `!unlink` au bot Aurvek dans Telegram pour supprimer immédiatement la liaison entre les deux comptes.

## Étapes

1. Ouvrez la conversation avec le bot, envoyez `!unlink`.
2. Le bot confirme : « Votre Telegram a été dissocié de votre compte. »
3. Il ne reconnaît plus ce compte ; tout nouveau message relance la procédure de liaison.

## Remarques

- Ni le compte Aurvek, ni ses conversations, ni le chat Telegram ne sont supprimés ; le bot cesse seulement de répondre comme utilisateur lié.
- Pour relier, envoyez un message et partagez à nouveau votre numéro.
- La dissociation utilisateur n’existe pas sur le web, seulement dans le bot.
- Un administrateur peut effacer le champ `telegram_chat_id` dans le panneau d’administration.

## Articles associés

- telegram_setup
- telegram_commands
