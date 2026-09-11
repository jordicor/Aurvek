---
id: settings_api_keys
title: "Utiliser vos propres clés API (BYOK)"
category: settings
keywords:
  - "clé API"
  - "BYOK"
  - "utiliser sa propre clé"
  - "OpenAI"
  - "Anthropic"
  - "Claude"
  - "Google AI"
  - "Gemini"
  - "xAI"
  - "Grok"
  - "ElevenLabs"
  - "MiniMax"
  - "Kimi"
  - "identifiants"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: fr
base_source_hash: 8d6ae4ed5c1d21ca604207d455d6d0683c5f58383e241db56f859b1f39e29dd1
---

## Réponse courte

Si votre compte le permet, configurez vos clés dans **Paramètres > Clés API** ou `/api-credentials`, choisissez le stockage et enregistrez. Elles remplacent les clés par défaut de la plateforme selon le mode du compte.

## Étapes

1. Ouvrez **Clés API** et choisissez **Cette session uniquement** (effacement à la fermeture de l’onglet), **Ce navigateur** (jusqu’à suppression) ou **Serveur sécurisé** (chiffrement, tous appareils).
2. Saisissez une ou plusieurs clés : OpenAI (GPT, image, voix), Anthropic (Claude), Google AI (Gemini), xAI (Grok), MiniMax, Kimi ou ElevenLabs (TTS et clonage vocal).
3. Utilisez **Tester**, **Tout tester**, puis **Tout enregistrer**.
4. Pour supprimer, cliquez sur le X du fournisseur puis enregistrez, ou utilisez **Tout effacer**.

## Remarques

- L’administrateur choisit : **Clés système uniquement**, **Clés personnelles uniquement**, **Les deux (préférer les personnelles)** ou **Les deux (préférer le système)**.
- En mode système uniquement, un message remplace la configuration. Si les clés personnelles sont obligatoires et absentes, une bannière avertit.
- Chaque fournisseur propose un lien **Obtenir une clé API**.

## Articles associés

- settings_profile
- settings_billing
