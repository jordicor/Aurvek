---
id: web_search_modes
title: "Comprendre les modes de recherche web"
category: search
keywords:
  - "recherche native"
  - "Perplexity"
  - "mode de recherche"
  - "moteur de recherche"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: fr
base_source_hash: 632298e8e9d58cedacdccbd71347370159ab69fa6929922203f4a729c8c9b12d
---

## Réponse courte

Aurvek offre **Native**, recommandé et intégré au modèle, et **Perplexity**, service externe. Choisissez-les dans les paramètres du compte.

## Étapes

1. Ouvrez **Paramètres**, puis **Recherche web**.
2. Choisissez **Native** (plus rapide, utilise la recherche intégrée au modèle et son contexte) ou **Perplexity** (service sonar-pro séparé, désactivé si le serveur ne le configure pas).
3. Enregistrez ; le changement s’applique au message suivant.

## Remarques

- Native fonctionne avec Claude, GPT et xAI. Un modèle sans recherche native, tel que Gemini, bascule automatiquement sur Perplexity.
- En Native, le modèle recherche et intègre directement les résultats, souvent avec citations.
- En Perplexity, le modèle appelle l’outil, récupère les résultats puis formule sa réponse : deux étapes.
- La préférence vaut pour toutes les conversations ; un prompt peut imposer ou interdire la recherche.

## Articles associés

- web_search_usage
