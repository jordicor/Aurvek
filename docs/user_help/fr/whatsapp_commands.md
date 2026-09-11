---
id: whatsapp_commands
title: "Commandes WhatsApp disponibles"
category: whatsapp
keywords:
  - "commandes WhatsApp"
  - "aide WhatsApp"
  - "mode texte"
  - "mode voix"
  - "changer de prompt"
  - "nouvelle conversation"
  - "liste de prompts"
  - "lister les conversations"
  - "changer de conversation"
prerequisites:
  - "Une conversation WhatsApp active affectée à votre compte"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: fr
base_source_hash: a88730b3fdf5473fa22b95fecca28ab8a93e10bf1b964484b89d9d9bd31db2df
---

## Réponse courte

Les commandes WhatsApp contrôlent la conversation sans quitter le chat. Elles commencent par `!` et ignorent la casse.

## Étapes

1. `!help` liste les commandes. `!text` active les réponses écrites (`text mode`, `text_mode`) ; `!voice` l’audio (`voice mode`, `voice_mode`).
2. `!prompt list` affiche jusqu’à 20 prompts avec ID et nom. `!prompt <name or id>` change par ID numérique, nom exact puis nom partiel, par exemple `!prompt 42` ou `!prompt email campaigns`.
3. `!new` crée une conversation et conserve l’ancienne sur le web.
4. `!chats` liste ID, titre, messages, date et badges ; `->` marque l’active.
5. `!set <id> [platform]` change l’affectation : `!set 1234`, `!set 1234 telegram`, alias `wa`/`tg`, ID avec ou sans `#`.

## Remarques

- Les commandes sont traitées avant l’IA et ne lui sont jamais transmises.
- Dans une conversation verrouillée, les messages normaux sont bloqués ; utilisez `!new`.
- Le mode voix utilise la TTS et peut débiter du solde selon le forfait.
- La commande `!prompt` essaie l’ID exact, puis le nom exact et enfin un nom partiel.

## Articles associés

- whatsapp_continue_conversation
- whatsapp_setup_phone
- external_manage_conversations
