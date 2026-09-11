---
id: settings_billing
title: "Consulter le solde et ajouter des fonds"
category: billing
keywords:
  - "solde"
  - "facturation"
  - "paiement"
  - "ajouter des fonds"
  - "recharger"
  - "Stripe"
  - "utilisation"
  - "dépenses"
  - "coût"
  - "code de réduction"
  - "stockage"
  - "quota"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: fr
base_source_hash: e96e5bbf094f8e7ea5f52f3d25678bcbf9063a1b9612b455d37dead0db96a14e
---

## Réponse courte

Le solde et l’usage figurent sous **Utilisation et facturation**. Pour recharger, cliquez sur **Ajouter des fonds** ou ouvrez `/payment`, choisissez entre $5 et $500 et payez par Stripe.

## Étapes

1. Ouvrez **Paramètres > Utilisation et facturation** : consultez **Solde actuel**, puis filtrez sur 7, 30, 90 jours ou toute la période.
2. Examinez opérations, jetons, total, moyenne quotidienne, stockage/quota, répartition IA/TTS/STT/images/vidéo/domaines, tendance et activité récente.
3. Cliquez sur **Ajouter des fonds** ou allez à `/payment`. Choisissez $5, $10, $25, $50, $100 ou un montant personnalisé de $5 à $500.
4. Saisissez éventuellement un **Code de réduction**, cliquez sur **Appliquer**, vérifiez le total puis sur **Payer avec Stripe**. Au retour, le solde est crédité immédiatement.

## Remarques

- Stripe traite les paiements ; Aurvek ne stocke pas la carte.
- Une réduction de 100 % crédite sans redirection Stripe.
- Le solde s’affiche en dollars US avec trois décimales, par exemple $12.450.
- Le solde du Profil est en lecture seule. Le quota dépend du compte ; téléversements et médias générés utilisent l’espace.

## Articles associés

- settings_profile
- settings_api_keys
