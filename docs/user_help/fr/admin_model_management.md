---
id: admin_model_management
title: "Gérer les modèles d’IA et les catalogues des fournisseurs"
category: settings
keywords:
  - "activer des modèles"
  - "désactiver des modèles"
  - "sélection groupée"
  - "synchronisation fournisseur"
  - "découverte de modèles"
required_role: admin
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-07
locale: fr
base_source_hash: b9815cfdd623e6fea3152e66aef9e4c6a5f32ac62477b1a95a9119179dfb9350
---

## Réponse courte

L’ouverture de **Gestion LLM** ou **Synchronisation des fournisseurs** actualise automatiquement les catalogues vieux de plus de 24 heures. La disponibilité et les réglages manuels existants restent inchangés ; les nouveaux modèles sont ajoutés désactivés. Le résultat apparaît sans recharger la page.

## Étapes

1. Ouvrez **Tous les modèles**, attendez la vérification, puis filtrez par fournisseur, nom, vision ou disponibilité.
2. Cochez des lignes ou **Sélectionner tous les modèles affichés**, puis **Activer la sélection** ou **Désactiver la sélection**. L’interrupteur d’une ligne enregistre aussi immédiatement.
3. Les filtres, le tri et la page restent en place ; une ligne ne correspondant plus au filtre disparaît et est désélectionnée.
4. Dans **Synchronisation des fournisseurs**, choisissez un fournisseur et consultez **Nouveaux**, **Mis à jour**, **À jour** et **Locaux uniquement**.
5. **Rechercher des mises à jour** détecte les versions récentes. **Mettre à jour le catalogue** ajoute les nouveautés désactivées et actualise immédiatement les métadonnées. **Nouveau** signifie absent de Tous les modèles.
6. Cochez ou décochez les modèles, puis **Enregistrer les modifications** ; **Annuler les modifications** efface les changements en attente.

## Remarques

- Les modèles synchronisés peuvent être désactivés ; seuls les modèles manuels peuvent être supprimés.
- **À vérifier** signale des métadonnées incomplètes : contrôlez le prix avant activation.
- Les dates sont partagées entre sessions. Après un échec, le catalogue enregistré reste disponible ; nouvel essai après une heure ou immédiatement par mise à jour manuelle.
- Les modèles gérés automatiquement, notamment ceux d’un compte lié, ne sont pas modifiables ici.
- Un modèle absent de la réponse du fournisseur devient « local uniquement » sans suppression ni changement de disponibilité.

## Articles associés

- chat_model_unavailable
