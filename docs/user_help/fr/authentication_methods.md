---
id: authentication_methods
title: "Méthodes de connexion disponibles"
category: auth
keywords:
  - "connexion"
  - "mot de passe"
  - "lien magique"
  - "Google"
  - "OAuth"
  - "authentification"
  - "inscription"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: fr
base_source_hash: 933920d39b7aa102fa1679b27c1eec45d2aa0b9d4e69c73fa554f7fd3127ca8b
---

## Réponse courte

Aurvek propose le nom d’utilisateur avec mot de passe, le lien magique à usage unique et Google Sign-In. Les méthodes disponibles dépendent de la configuration de votre compte par l’administrateur.

## Étapes

1. Votre compte utilise **Lien magique uniquement**, **Mot de passe uniquement** ou **Lien magique + mot de passe**. Si Google OAuth est activé, **Se connecter avec Google** apparaît quel que soit ce mode.
2. Avec un mot de passe, ouvrez la connexion, saisissez nom d’utilisateur et mot de passe, puis cliquez sur **Se connecter**.
3. Avec un lien magique, ouvrez l’URL reçue. Elle expire après 3 jours ; utilisez `/magic-link-recovery` avec votre e-mail pour en recevoir une nouvelle.
4. Avec Google, choisissez **Se connecter avec Google**, sélectionnez le compte et autorisez Aurvek. Une adresse correspondant à un compte existant le lie ; sinon, un compte est créé automatiquement.
5. Pour vous inscrire, cliquez sur **S’inscrire**, remplissez les champs requis et validez. Google peut aussi servir à l’inscription lorsqu’il est disponible.

## Remarques

- Une session dure au maximum 30 jours.
- Après une inscription Google, Aurvek peut demander de définir un mot de passe après la première connexion.
- Si autorisé, modifiez-le dans **Paramètres > Profil > Modifier le mot de passe**.
- Cloudflare Turnstile ou Google reCAPTCHA peut protéger la page selon la configuration.
- L’administrateur choisit le mode ; contactez-le pour en changer.

## Articles associés

- settings_profile
