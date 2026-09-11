---
id: admin_model_management
title: Managing AI models and provider catalogs
category: settings
keywords:
  - enable models
  - disable models
  - bulk selection
  - provider sync
  - model discovery
  - activar modelos
  - seleccion multiple
required_role: admin
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-07
---

## Short answer

Opening **LLM Management** or **Provider Sync** automatically updates provider catalogs older than 24 hours. Existing availability and manual adjustments stay in place; new models are added disabled. The screen shows the result without reloading.

## Steps

1. Open **All Models**, wait for the catalog check, then filter by provider, name, vision or availability.
2. Check individual rows or **Select all shown models**. Choose **Enable Selected** or **Disable Selected**. The row switch also saves immediately.
3. Filters, sorting and page position stay in place. Rows that no longer match a filter disappear and are deselected.
4. Open **Provider Sync** and choose a provider. Review the **New**, **Updated**, **Up to date** and **Local only** counts and filters.
5. **Check for Updates** detects releases since the automatic update. **Update Catalog** adds them disabled and refreshes metadata immediately. **New** means not yet added to All Models.
6. To change availability in Provider Sync, check or uncheck models and click **Save Changes**. Use **Undo Changes** to reset pending edits before refreshing the catalog.

## Notes

- Synced models can be disabled; only manual models can be deleted.
- **Review** marks incomplete metadata; verify pricing before enabling the model.
- Update dates are shared across sessions. Failed providers keep their saved catalog and can retry on a later visit after one hour, or immediately with a manual update.
- Models managed automatically, such as linked-account models, cannot be switched here.
- A model absent from a provider response is marked local only; this does not automatically delete it or change availability.

## Related

- chat_model_unavailable
