---
id: telegram_commands
title: "Commandes du bot Telegram"
category: telegram
keywords:
  - "Telegram"
  - "commandes"
  - "bot"
  - "aide"
  - "texte"
  - "voix"
  - "prompt"
  - "nouvelle conversation"
  - "dissocier"
  - "lister les conversations"
  - "changer de conversation"
prerequisites:
  - "Un compte Telegram lié à Aurvek (voir telegram_setup)"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: fr
base_source_hash: 90283b72cdd9c9a04f67c402858c03345ac26776501d8a38c9552a6734298ec1
---

## Réponse courte

Le bot Telegram Aurvek accepte des commandes commençant par `!` pour le texte ou la voix, le prompt, les conversations et la dissociation. Tapez `!help` pour la liste.

## Étapes

1. `!help` affiche les commandes ; `!text` active le texte ; `!voice` la TTS avec retour au texte en cas d’échec.
2. `!prompt list` affiche jusqu’à 20 prompts avec ID et nom ; `!prompt <name or id>` choisit par ID, nom exact puis nom partiel.
3. `!new` crée une conversation et conserve l’ancienne sur le web ; `!unlink` dissocie Telegram.
4. `!chats` liste jusqu’à 15 conversations avec ID, titre, messages et dernière activité ; `->` marque l’active.
5. `!set <id> [platform]` change l’affectation : `!set 1234`, `!set 1234 whatsapp`, alias `wa`/`tg`, avec `#` facultatif.

## Remarques

- La casse est ignorée : `!Help`, `!HELP` et `!help` sont équivalents.
- Vous pouvez envoyer texte, voix ou photos ; les voix sont transcrites automatiquement.
- Si la conversation est verrouillée, utilisez `!new`.
- Les longues réponses sont découpées selon les limites Telegram.
- La commande `!prompt` essaie l’ID exact, puis le nom exact et enfin un nom partiel.

## Articles associés

- telegram_setup
- telegram_unlink
- external_manage_conversations
